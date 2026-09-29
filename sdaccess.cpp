#include "sdaccess.h"

namespace {
SemaphoreHandle_t sdMutex = nullptr;
portMUX_TYPE initMutex = portMUX_INITIALIZER_UNLOCKED;
}

bool lockSD()
{
    if (!sdMutex) {
        SemaphoreHandle_t candidate = xSemaphoreCreateRecursiveMutex();
        portENTER_CRITICAL(&initMutex);
        if (!sdMutex) {
            sdMutex = candidate;
            candidate = nullptr;
        }
        portEXIT_CRITICAL(&initMutex);
        if (candidate) vSemaphoreDelete(candidate);
    }
    return sdMutex && xSemaphoreTakeRecursive(sdMutex, pdMS_TO_TICKS(2000)) == pdTRUE;
}

void unlockSD()
{
    xSemaphoreGiveRecursive(sdMutex);
}
