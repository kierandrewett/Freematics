#pragma once

#include "FreematicsBase.h"
#include <mbedtls/ssl.h>
#include <mbedtls/entropy.h>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/x509_crt.h>

// TLS runs on the ESP32. The modem supplies a cellular TCP socket only.
class CellularTLS {
public:
    CellularTLS();
    ~CellularTLS();
    void begin(CFreematics* device) { m_device = device; }
    bool synchroniseClock(const char* host);
    bool open(const char* host, uint16_t port, const char* rootCA);
    void close();
    bool write(const char* data, size_t length);
    bool response(char* body, size_t capacity, uint16_t* code, int* length, unsigned timeout);
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
    mbedtls_ssl_context m_ssl;
    mbedtls_ssl_config m_config;
    mbedtls_entropy_context m_entropy;
    mbedtls_ctr_drbg_context m_random;
    mbedtls_x509_crt m_ca;
};
