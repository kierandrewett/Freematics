CC=gcc
CFLAGS=-O3 -Wunused-result
HEADERS = httpint.h httpapi.h capture_inbox.h
TARGET = teleserver
OBJS += teleserver.o capture_inbox.o udpserver.o teletrips.o telebroker.o data2kml.o processpil.o httpd/httppil.o httpd/httpd.o cJSON/cJSON.o cJSON/cJSON_Utils.o libb64/cdecode.o libb64/cencode.o jsonconfig.o

CFLAGS+=-DMAX_CHANNELS=16
CFLAGS+=-Ihttpd -Ilibb64 -IcJSON
LDFLAGS = -lm
# zlib inflates compressed device batches. The production image has only the
# runtime library, so link the shared object directly when no dev symlink exists.
ZLIB_LINK := $(firstword $(wildcard /usr/lib/libz.so /usr/lib64/libz.so /lib/libz.so /usr/lib/libz.so.1 /lib/libz.so.1 /usr/lib64/libz.so.1))
LDFLAGS += $(if $(ZLIB_LINK),$(ZLIB_LINK),-lz)

ifdef WINDIR
CFLAGS+= -DWIN32
LDFLAGS += -lwsock32
OS="Win32"
else
#CFLAGS+= -fPIC
OS="Linux"
endif

ifndef DEBUG
LDFLAGS += -s
else
CFLAGS+= -DHTTP_DEBUG
LDFLAGS += -g
endif

%.o: %.c $(HEADERS)
	$(CC) -c -o $@ $(CFLAGS) $(filter %.c, $^)

all: $(OBJS)
	@echo Building for $(OS)
	$(CC) $(OBJS) -o $(TARGET) $(LDFLAGS)

install: all
	@rm -f /usr/bin/$(TARGET)
	@cp $(TARGET) /usr/bin

clean:
	@rm -f $(TARGET) $(TARGET).exe
	@rm -f *.o
	@rm -rf Debug Release
