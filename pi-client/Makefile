# Pi 常駐表示クライアントのビルド。
# rpi-rgb-led-matrix のソースツリーを RGB_LIB_DISTRIBUTION で指定する。
RGB_LIB_DISTRIBUTION ?= $(HOME)/rpi-rgb-led-matrix
RGB_INCDIR = $(RGB_LIB_DISTRIBUTION)/include
RGB_LIBDIR = $(RGB_LIB_DISTRIBUTION)/lib
RGB_LIBRARY_NAME = rgbmatrix
RGB_LIBRARY = $(RGB_LIBDIR)/lib$(RGB_LIBRARY_NAME).a

CXXFLAGS ?= -O3 -W -Wall -Wextra -Wno-unused-parameter -std=c++17
LDFLAGS ?= -L$(RGB_LIBDIR) -l$(RGB_LIBRARY_NAME) -lrt -lm -lpthread -lz

all: pi_client

pi_client: pi_client.o $(RGB_LIBRARY)
	$(CXX) $< -o $@ $(LDFLAGS)

pi_client.o: pi_client.cc panel_calibration.h
	$(CXX) -I$(RGB_INCDIR) $(CXXFLAGS) -c -o $@ $<

panel_calibration_selftest: panel_calibration_selftest.cc panel_calibration.h
	$(CXX) $(CXXFLAGS) -o $@ $<

$(RGB_LIBRARY):
	$(MAKE) -C $(RGB_LIBDIR)

clean:
	rm -f pi_client pi_client.o panel_calibration_selftest

.PHONY: all clean panel_calibration_selftest
