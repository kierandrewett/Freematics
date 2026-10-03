#pragma once

#include "FreematicsBase.h"
#include <mbedtls/ssl.h>
#include <mbedtls/entropy.h>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/x509_crt.h>

// TLS runs on the ESP32. The modem supplies a cellular TCP socket only.
class CellularTLS {
public:
    static const size_t HTTP_LOCATION_CAPACITY = 2048;
    struct HTTPResponseInfo {
        uint16_t status;
        uint32_t contentLength;
        char location[HTTP_LOCATION_CAPACITY];
    };
    typedef bool (*HTTPBodyWriter)(void* context, const unsigned char* data, size_t length);
    typedef bool (*HTTPContinueCheck)(void* context);

    CellularTLS();
    ~CellularTLS();
    void begin(CFreematics* device) { m_device = device; }
    bool synchroniseClock(const char* host);
    bool open(const char* host, uint16_t port, const char* rootCA);
    void close();
    bool write(const char* data, size_t length);
    void setContinueCheck(HTTPContinueCheck check, void* context)
        { m_continueCheck = check; m_continueContext = context; }
    bool response(char* body, size_t capacity, uint16_t* code, int* length, unsigned timeout);
    // For non-200 responses, returns parsed metadata without consuming the
    // body; the caller must close the connection before another request.
    bool streamResponse(uint32_t maxContentLength, HTTPBodyWriter writer, void* context,
        HTTPResponseInfo* info, unsigned timeout);
private:
    bool command(const char* text, unsigned timeout = 5000, const char* expected = nullptr);
    bool socketOpen(const char* host, uint16_t port);
    int socketWrite(const unsigned char* data, size_t length);
    int socketRead(unsigned char* data, size_t length);
    int read(unsigned char* data, size_t length);
    bool line(char* data, size_t capacity);
    static int sendBytes(void* context, const unsigned char* data, size_t length);
    static int receiveBytes(void* context, unsigned char* data, size_t length);
    CFreematics* m_device = nullptr;
    char m_reply[512];
    bool m_connected = false;
    bool m_socketClosed = true;
    uint32_t m_deadline = 0;
    HTTPContinueCheck m_continueCheck = nullptr;
    void* m_continueContext = nullptr;
    mbedtls_ssl_context m_ssl;
    mbedtls_ssl_config m_config;
    mbedtls_entropy_context m_entropy;
    mbedtls_ctr_drbg_context m_random;
    mbedtls_x509_crt m_ca;
};
