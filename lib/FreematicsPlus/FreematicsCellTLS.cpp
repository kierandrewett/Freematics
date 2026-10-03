#include "FreematicsCellTLS.h"
#include <mbedtls/net_sockets.h>
#include <sys/time.h>
#include <time.h>
#include <ctype.h>

#ifndef CELL_TLS_CHUNK_BYTES
#define CELL_TLS_CHUNK_BYTES 1024
#endif
#ifndef CELL_TLS_COMMAND_SETTLE_MS
#define CELL_TLS_COMMAND_SETTLE_MS 10
#endif
#ifndef CELL_TLS_HTTP_BODY_CHUNK_BYTES
#define CELL_TLS_HTTP_BODY_CHUNK_BYTES 256
#endif

namespace {
bool timeValid(time_t value) { return value >= 1704067200 && value <= 2145916799; }
int hexDigit(char value)
{
    if (value >= '0' && value <= '9') return value - '0';
    if (value >= 'A' && value <= 'F') return value - 'A' + 10;
    if (value >= 'a' && value <= 'f') return value - 'a' + 10;
    return -1;
}

bool certificateDatesValid(const mbedtls_x509_crt* certificate)
{
    // The Arduino SDK can build mbedTLS without automatic date validation.
    // Compare every certificate's dates explicitly with the ESP32 UTC clock.
    time_t now = time(nullptr);
    struct tm utc;
    if (!timeValid(now) || !gmtime_r(&now, &utc) || !certificate) return false;
    const uint64_t current = (((((uint64_t)(utc.tm_year + 1900) * 100 + utc.tm_mon + 1) * 100 + utc.tm_mday) * 100 + utc.tm_hour) * 100 + utc.tm_min) * 100 + utc.tm_sec;
    auto stamp = [](const mbedtls_x509_time& date) -> uint64_t {
        return (((((uint64_t)date.year * 100 + date.mon) * 100 + date.day) * 100 + date.hour) * 100 + date.min) * 100 + date.sec;
    };
    for (; certificate; certificate = certificate->next) {
        if (current < stamp(certificate->valid_from) || current > stamp(certificate->valid_to)) return false;
    }
    return true;
}
}

CellularTLS::CellularTLS()
{
    mbedtls_ssl_init(&m_ssl);
    mbedtls_ssl_config_init(&m_config);
    mbedtls_entropy_init(&m_entropy);
    mbedtls_ctr_drbg_init(&m_random);
    mbedtls_x509_crt_init(&m_ca);
}

CellularTLS::~CellularTLS()
{
    close();
    mbedtls_ssl_free(&m_ssl);
    mbedtls_ssl_config_free(&m_config);
    mbedtls_entropy_free(&m_entropy);
    mbedtls_ctr_drbg_free(&m_random);
    mbedtls_x509_crt_free(&m_ca);
}

bool CellularTLS::command(const char* text, unsigned timeout, const char* expected)
{
    if (!m_device) return false;
    if (text) {
        // Socket data stays in the modem in manual receive mode. Discard
        // stale UART responses from the previous command or ESP32 restart.
        m_device->xbPurge();
        m_device->xbWrite(text);
        // xbReceive() below waits for the reply, so this pause only adds
        // latency to every AT command; it stays configurable for bench tests.
        if (CELL_TLS_COMMAND_SETTLE_MS) delay(CELL_TLS_COMMAND_SETTLE_MS);
    }
    m_reply[0] = 0;
    const char* answers[] = {"\r\nOK", "\r\nERROR"};
    int result = m_device->xbReceiveCancellable(m_reply, sizeof(m_reply), timeout,
        expected ? &expected : answers, expected ? 1 : 2,
        m_continueCheck, m_continueContext);
    if (strstr(m_reply, "+IPCLOSE: 0,")) m_socketClosed = true;
    return result == 1;
}

bool CellularTLS::socketOpen(const char* host, uint16_t port)
{
    if (!host || !*host || strlen(host) > 253 || strchr(host, '"') || strchr(host, '\r') || strchr(host, '\n')) return false;
    close();
    if (!command("AT+CIPRXGET=1\r")) return false;
    char text[320];
    snprintf(text, sizeof(text), "AT+CIPOPEN=0,\"TCP\",\"%s\",%u\r", host, port);
    if (!command(text, 30000, "+CIPOPEN:")) return false;
    const char* result = strstr(m_reply, "+CIPOPEN:");
    int link = -1, error = -1;
    if (!result || sscanf(result, "+CIPOPEN: %d,%d", &link, &error) != 2 || link != 0 || error != 0) return false;
    m_socketClosed = false;
    return true;
}

void CellularTLS::close()
{
    m_connected = false;
    if (m_device) command("AT+CIPCLOSE=0\r", 3000, "+CIPCLOSE:");
    m_socketClosed = true;
}

int CellularTLS::socketWrite(const unsigned char* data, size_t length)
{
    // Each chunk costs one AT+CIPSEND round trip, so larger chunks move a
    // batch in fewer round trips. The SIM7670 accepts up to 1500 bytes.
    const size_t count = min(length, (size_t)CELL_TLS_CHUNK_BYTES);
    char text[48];
    snprintf(text, sizeof(text), "AT+CIPSEND=0,%u\r", (unsigned)count);
    if (m_socketClosed || !command(text, 5000, ">")) return MBEDTLS_ERR_NET_SEND_FAILED;
    m_device->xbWrite((const char*)data, count);
    if (!command(nullptr, 20000, "+CIPSEND:")) return MBEDTLS_ERR_NET_SEND_FAILED;
    const char* reply = strstr(m_reply, "+CIPSEND:");
    unsigned link, requested, sent;
    if (!reply || sscanf(reply, "+CIPSEND: %u,%u,%u", &link, &requested, &sent) != 3 ||
        link != 0 || requested != count || sent != count) return MBEDTLS_ERR_NET_SEND_FAILED;
    return count;
}

int CellularTLS::socketRead(unsigned char* data, size_t length)
{
    // Hex mode keeps NUL and CR/LF in TLS records out of the AT line parser.
    // 128 bytes plus the response header fit in its 512-byte buffer.
    const size_t count = min(length, (size_t)128);
    while ((int32_t)(m_deadline - millis()) > 0) {
        if (m_continueCheck && !m_continueCheck(m_continueContext))
            return MBEDTLS_ERR_NET_RECV_FAILED;
        if (!command("AT+CIPRXGET=4,0\r")) return MBEDTLS_ERR_NET_RECV_FAILED;
        const char* reply = strstr(m_reply, "+CIPRXGET: 4,0,");
        if (!reply) return MBEDTLS_ERR_NET_RECV_FAILED;
        if (strtoul(reply + strlen("+CIPRXGET: 4,0,"), nullptr, 10) == 0) {
            if (m_socketClosed) return 0;
            delay(100);
            continue;
        }
        char text[48];
        snprintf(text, sizeof(text), "AT+CIPRXGET=3,0,%u\r", (unsigned)count);
        if (!command(text)) return MBEDTLS_ERR_NET_RECV_FAILED;
        reply = strstr(m_reply, "+CIPRXGET: 3,0,");
        unsigned received, remaining;
        if (!reply || sscanf(reply, "+CIPRXGET: 3,0,%u,%u", &received, &remaining) != 2 || received > count) return MBEDTLS_ERR_NET_RECV_FAILED;
        reply = strchr(reply, '\n');
        if (!reply || strlen(++reply) < received * 2) return MBEDTLS_ERR_NET_RECV_FAILED;
        for (unsigned index = 0; index < received; index++) {
            int high = hexDigit(reply[index * 2]), low = hexDigit(reply[index * 2 + 1]);
            if (high < 0 || low < 0) return MBEDTLS_ERR_NET_RECV_FAILED;
            data[index] = (high << 4) | low;
        }
        return received;
    }
    return MBEDTLS_ERR_SSL_WANT_READ;
}

int CellularTLS::sendBytes(void* context, const unsigned char* data, size_t length)
{
    return static_cast<CellularTLS*>(context)->socketWrite(data, length);
}

int CellularTLS::receiveBytes(void* context, unsigned char* data, size_t length)
{
    return static_cast<CellularTLS*>(context)->socketRead(data, length);
}

bool CellularTLS::synchroniseClock(const char* host)
{
    // Bootstrap the ESP32 clock from the same server's HTTP Date header.
    // This carries no credentials. The subsequent TLS connection verifies
    // the root CA, hostname and certificate dates before sending data.
    if (!socketOpen(host, 80)) return timeValid(time(nullptr));
    String request = String("GET / HTTP/1.1\r\nHost: ") + host + "\r\nConnection: close\r\n\r\n";
    bool sent = socketWrite((const unsigned char*)request.c_str(), request.length()) == (int)request.length();
    char headers[1024] = {0};
    size_t bytes = 0;
    m_deadline = millis() + 15000;
    while (sent && bytes < sizeof(headers) - 1 && !strstr(headers, "\r\n\r\n")) {
        int received = socketRead((unsigned char*)headers + bytes, min((size_t)128, sizeof(headers) - 1 - bytes));
        if (received <= 0) break;
        bytes += received;
        headers[bytes] = 0;
    }
    close();
    for (char* line = headers; line && *line;) {
        if (!strncasecmp(line, "date: ", 6)) {
            struct tm date = {};
            char* end = strptime(line + 6, "%a, %d %b %Y %H:%M:%S GMT", &date);
            if (end && !strncmp(end, "\r\n", 2)) {
                time_t value = mktime(&date); // ESP32 system timezone is UTC.
                if (timeValid(value)) {
                    struct timeval clock = {value, 0};
                    if (settimeofday(&clock, nullptr) == 0) {
                        freematicsMarkSystemTimeTrusted();
                        Serial.println("[TIME] ESP32 UTC refreshed over cellular TCP");
                        return true;
                    }
                    Serial.println("[TIME] Could not set ESP32 clock from cellular Date header");
                }
            }
        }
        line = strstr(line, "\r\n");
        if (line) line += 2;
    }
    return timeValid(time(nullptr));
}

bool CellularTLS::open(const char* host, uint16_t port, const char* rootCA)
{
    if (!timeValid(time(nullptr)) || !socketOpen(host, port)) return false;
    mbedtls_ssl_free(&m_ssl); mbedtls_ssl_init(&m_ssl);
    mbedtls_ssl_config_free(&m_config); mbedtls_ssl_config_init(&m_config);
    mbedtls_x509_crt_free(&m_ca); mbedtls_x509_crt_init(&m_ca);
    const char* seed = "freematics-cellular-tls";
    int result = mbedtls_ctr_drbg_seed(&m_random, mbedtls_entropy_func, &m_entropy,
        (const unsigned char*)seed, strlen(seed));
    if (!result) result = mbedtls_x509_crt_parse(&m_ca, (const unsigned char*)rootCA, strlen(rootCA) + 1);
    if (!result) result = mbedtls_ssl_config_defaults(&m_config, MBEDTLS_SSL_IS_CLIENT,
        MBEDTLS_SSL_TRANSPORT_STREAM, MBEDTLS_SSL_PRESET_DEFAULT);
    mbedtls_ssl_conf_authmode(&m_config, MBEDTLS_SSL_VERIFY_REQUIRED);
    mbedtls_ssl_conf_min_version(&m_config, MBEDTLS_SSL_MAJOR_VERSION_3, MBEDTLS_SSL_MINOR_VERSION_3);
    mbedtls_ssl_conf_ca_chain(&m_config, &m_ca, nullptr);
    mbedtls_ssl_conf_rng(&m_config, mbedtls_ctr_drbg_random, &m_random);
    if (!result) result = mbedtls_ssl_setup(&m_ssl, &m_config);
    if (!result) result = mbedtls_ssl_set_hostname(&m_ssl, host);
    mbedtls_ssl_set_bio(&m_ssl, this, sendBytes, receiveBytes, nullptr);
    m_deadline = millis() + 60000;
    while (!result) {
        if (m_continueCheck && !m_continueCheck(m_continueContext)) {
            close();
            return false;
        }
        result = mbedtls_ssl_handshake(&m_ssl);
        if (!result) break;
        if ((result == MBEDTLS_ERR_SSL_WANT_READ || result == MBEDTLS_ERR_SSL_WANT_WRITE) &&
            (int32_t)(m_deadline - millis()) > 0) { result = 0; delay(10); }
    }
    const uint32_t verification = mbedtls_ssl_get_verify_result(&m_ssl);
    const bool datesValid = certificateDatesValid(mbedtls_ssl_get_peer_cert(&m_ssl)) && certificateDatesValid(&m_ca);
    if (result || verification || !datesValid) {
        Serial.printf("[TLS] Cellular verification failed: error=%d flags=%lu dates_valid=%d\n", result, (unsigned long)verification, datesValid);
        close();
        return false;
    }
    m_connected = true;
    Serial.println("[TLS] Cellular ESP32 certificate, hostname and dates verified");
    return true;
}

bool CellularTLS::write(const char* data, size_t length)
{
    if (!m_connected) return false;
    m_deadline = millis() + 30000;
    while (length && (int32_t)(m_deadline - millis()) > 0) {
        int sent = mbedtls_ssl_write(&m_ssl, (const unsigned char*)data, length);
        if (sent == MBEDTLS_ERR_SSL_WANT_READ || sent == MBEDTLS_ERR_SSL_WANT_WRITE) { delay(10); continue; }
        if (sent <= 0) return false;
        data += sent;
        length -= sent;
    }
    return length == 0;
}

int CellularTLS::read(unsigned char* data, size_t length)
{
    while ((int32_t)(m_deadline - millis()) > 0) {
        int count = mbedtls_ssl_read(&m_ssl, data, length);
        if (count == MBEDTLS_ERR_SSL_WANT_READ || count == MBEDTLS_ERR_SSL_WANT_WRITE) { delay(10); continue; }
        return count;
    }
    return -1;
}

bool CellularTLS::line(char* data, size_t capacity)
{
    size_t bytes = 0;
    while (bytes < capacity - 1) {
        if (read((unsigned char*)data + bytes, 1) != 1) return false;
        if (data[bytes++] == '\n') {
            if (bytes < 2 || data[bytes - 2] != '\r') return false;
            data[bytes - 2] = 0;
            return true;
        }
    }
    return false;
}

bool CellularTLS::response(char* body, size_t capacity, uint16_t* code, int* length, unsigned timeout)
{
    if (!m_connected || capacity < 1) return false;
    m_deadline = millis() + max(timeout, 15000U);
    char header[512];
    unsigned status;
    if (!line(header, sizeof(header)) || sscanf(header, "HTTP/1.%*u %u", &status) != 1) return false;
    *code = status;
    int contentLength = -1;
    bool chunked = false, ended = false;
    for (unsigned lines = 0; lines < 40; lines++) {
        if (!line(header, sizeof(header))) return false;
        if (!*header) { ended = true; break; }
        if (!strncasecmp(header, "content-length:", 15)) {
            char* end;
            unsigned long value = strtoul(header + 15, &end, 10);
            if (*end || end == header + 15 || value >= capacity || contentLength >= 0) return false;
            contentLength = value;
        } else if (!strncasecmp(header, "transfer-encoding:", 18)) {
            const char* value = header + 18;
            while (*value == ' ') value++;
            if (strcasecmp(value, "chunked")) return false;
            chunked = true;
        }
    }
    if (!ended || (chunked && contentLength >= 0) || (!chunked && contentLength < 0)) return false;
    size_t bytes = 0;
    do {
        size_t count = contentLength < 0 ? 0 : contentLength;
        if (chunked) {
            if (!line(header, sizeof(header))) return false;
            char* end;
            count = strtoul(header, &end, 16);
            if (end == header || (*end && *end != ';')) return false;
            if (count == 0) {
                for (unsigned lines = 0; lines < 40; lines++) {
                    if (!line(header, sizeof(header))) return false;
                    if (!*header) { body[bytes] = 0; if (length) *length = bytes; return true; }
                }
                return false;
            }
        }
        if (count >= capacity - bytes) return false;
        while (count) {
            int received = read((unsigned char*)body + bytes, count);
            if (received <= 0) return false;
            bytes += received;
            count -= received;
        }
        if (chunked && (!line(header, sizeof(header)) || *header)) return false;
    } while (chunked);
    body[bytes] = 0;
    if (length) *length = bytes;
    return true;
}

bool CellularTLS::streamResponse(uint32_t maxContentLength, HTTPBodyWriter writer, void* context,
    HTTPResponseInfo* info, unsigned timeout)
{
    if (!m_connected || !writer || !info) return false;
    memset(info, 0, sizeof(*info));
    m_deadline = millis() + max(timeout, 15000U);

    // Keep both an individual line and aggregate headers strictly bounded.
    static const size_t HEADER_LINE_CAPACITY = 2048;
    static const size_t HEADER_BYTES_LIMIT = 8192;
    static const unsigned HEADER_LINE_LIMIT = 64;
    char header[HEADER_LINE_CAPACITY];
    size_t headerBytes = 0;
    auto readHeaderLine = [this](char* output, size_t capacity, size_t& length) -> bool {
        length = 0;
        while (length + 1 < capacity) {
            unsigned char ch;
            if (read(&ch, 1) != 1 || ch == 0) return false;
            if (ch == '\n') {
                if (!length || output[length - 1] != '\r') return false;
                length--;
                output[length] = 0;
                return true;
            }
            output[length++] = (char)ch;
        }
        return false;
    };
    unsigned status = 0;
    size_t statusLineLength = 0;
    if (!readHeaderLine(header, sizeof(header), statusLineLength)) return false;
    headerBytes += statusLineLength + 2;
    if (headerBytes > HEADER_BYTES_LIMIT || statusLineLength < 12 || strncmp(header, "HTTP/1.", 7) ||
        (header[7] != '0' && header[7] != '1') || header[8] != ' ' ||
        !isdigit((unsigned char)header[9]) || !isdigit((unsigned char)header[10]) ||
        !isdigit((unsigned char)header[11]) || (header[12] && header[12] != ' ')) return false;
    for (size_t i = 12; i < statusLineLength; i++)
        if (((unsigned char)header[i] < 0x20 && header[i] != '\t') || (unsigned char)header[i] == 0x7f) return false;
    status = (unsigned)(header[9] - '0') * 100 + (unsigned)(header[10] - '0') * 10 + (unsigned)(header[11] - '0');
    if (status < 100 || status > 599) return false;

    bool haveLength = false;
    bool haveLocation = false;
    bool haveTransferEncoding = false;
    uint32_t bodyLength = 0;
    bool ended = false;
    for (unsigned count = 0; count < HEADER_LINE_LIMIT; count++) {
        size_t lineLength = 0;
        if (!readHeaderLine(header, sizeof(header), lineLength)) return false;
        headerBytes += lineLength + 2;
        if (headerBytes > HEADER_BYTES_LIMIT) return false;
        if (!lineLength) { ended = true; break; }

        // Reject malformed field syntax and control characters before interpreting values.
        char* colon = strchr(header, ':');
        if (!colon || colon == header) return false;
        for (char* p = header; p < colon; p++) {
            unsigned char c = (unsigned char)*p;
            if (!(isalnum(c) || strchr("!#$%&'*+-.^_`|~", c))) return false;
        }
        for (const unsigned char* p = (const unsigned char*)colon + 1; *p; p++)
            if ((*p < 0x20 && *p != '\t') || *p == 0x7f) return false;

        const char* value = colon + 1;
        while (*value == ' ' || *value == '\t') value++;
        char* valueEnd = header + lineLength;
        while (valueEnd > value && (valueEnd[-1] == ' ' || valueEnd[-1] == '\t')) *--valueEnd = 0;

        size_t nameLength = colon - header;
        if (nameLength == 14 && !strncasecmp(header, "Content-Length", nameLength)) {
            if (haveLength || !*value) return false;
            uint32_t parsed = 0;
            for (const char* p = value; *p; p++) {
                if (!isdigit((unsigned char)*p)) return false;
                unsigned digit = (unsigned)(*p - '0');
                if (parsed > (UINT32_MAX - digit) / 10) return false;
                parsed = parsed * 10 + digit;
            }
            // Redirect/error bodies are never consumed by this API, so their
            // advertised length is unrelated to the caller's asset-size cap.
            if (status == 200 && parsed > maxContentLength) return false;
            bodyLength = parsed;
            haveLength = true;
        } else if (nameLength == 17 && !strncasecmp(header, "Transfer-Encoding", nameLength)) {
            // This primitive intentionally supports only a fixed Content-Length body.
            // Error/redirect bodies are not consumed, so their framing is irrelevant.
            if (haveTransferEncoding || status == 200) return false;
            haveTransferEncoding = true;
        } else if (nameLength == 8 && !strncasecmp(header, "Location", nameLength)) {
            if (haveLocation || strlen(value) >= sizeof(info->location)) return false;
            memcpy(info->location, value, strlen(value) + 1);
            haveLocation = true;
        }
    }
    if (!ended || (status == 200 && (!haveLength || haveTransferEncoding))) return false;
    if (!haveLength) bodyLength = 0;

    info->status = (uint16_t)status;
    info->contentLength = bodyLength;
    // Redirect and error bodies are not firmware bytes. Return their bounded
    // metadata so the caller can validate Location/status, then close this
    // connection without accidentally feeding that body to the OTA writer.
    if (status != 200) return true;

    unsigned char body[ CELL_TLS_HTTP_BODY_CHUNK_BYTES ];
    uint32_t remaining = bodyLength;
    while (remaining) {
        size_t wanted = remaining < sizeof(body) ? remaining : sizeof(body);
        int received = read(body, wanted);
        if (received <= 0 || (size_t)received > wanted) return false;
        if (!writer(context, body, (size_t)received)) return false;
        remaining -= (uint32_t)received;
    }

    return true;
}
