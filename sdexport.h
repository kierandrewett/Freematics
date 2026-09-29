#ifndef SDEXPORT_H_INCLUDED
#define SDEXPORT_H_INCLUDED

#include "config.h"

#if STORAGE == STORAGE_SD
void processSDExport();
void resetSDExportDirectory();
#endif

#endif
