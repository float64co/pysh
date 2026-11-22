# pysh: one binary embedding the system's CPython.
#   make          pysh         libpython linked in, glibc dynamic, Py* symbols exported, so every
#                              extension .so your `python` can load (ssl, sqlite3, lxml, numpy...) works
#   make static   pysh-static  fully static, but cannot load C-extension modules
# Stdlib / site-packages come from the same prefix as the `python` on PATH
# (PYSH_HOME, baked in at build time; override at run time with PYSH_HOME).
PYTHON ?= python3
CFLAGS ?= -O2
PYINC  := $(shell $(PYTHON)-config --includes)
PYLIBS := $(shell $(PYTHON)-config --ldflags --embed)
PYHOME := $(shell $(PYTHON) -c 'import sys;print(sys.base_prefix)')
LIBPL  := $(shell $(PYTHON) -c 'import sysconfig;print(sysconfig.get_config_var("LIBPL"))')
LIBPYA := $(LIBPL)/$(shell $(PYTHON) -c 'import sysconfig;print(sysconfig.get_config_var("LIBRARY"))')
SRC    := pysh.c sha2_shim.c pysh_py.h
CC_PY   = $(CC) $(CFLAGS) -Wall -DPYSH_HOME='"$(PYHOME)"' -o $@ pysh.c sha2_shim.c $(PYINC)

pysh: $(SRC)
	$(CC_PY) -no-pie -rdynamic -Wl,--whole-archive $(LIBPYA) -Wl,--no-whole-archive \
	  -lexpat -lz -lcrypto -lcrypt -lutil -ldl -lpthread -lm
	strip $@

static: pysh-static
pysh-static: $(SRC)
	$(CC_PY) -static $(PYLIBS) -lexpat -lz -lcrypto -lcrypt -lutil -ldl -lpthread -lm
	strip $@

pysh_py.h: pysh.py
	( xxd -i -n pysh_py_bytes $< | sed 's/^unsigned char/static const unsigned char/;/_len =/d'; \
	  echo 'static const unsigned char *const pysh_py = pysh_py_bytes;' ) > $@.tmp
	sed -i 's/}; *$$/, 0};/' $@.tmp 2>/dev/null; mv $@.tmp $@

PREFIX ?= /usr/local
install: pysh
	install -Dm755 pysh $(DESTDIR)$(PREFIX)/bin/pysh
	install -Dm644 pysh.1 $(DESTDIR)$(PREFIX)/share/man/man1/pysh.1

install-static: pysh-static
	install -Dm755 pysh-static $(DESTDIR)$(PREFIX)/bin/pysh
	install -Dm644 pysh.1 $(DESTDIR)$(PREFIX)/share/man/man1/pysh.1

clean:
	rm -f pysh pysh-static pysh-dynamic pysh_py.h
.PHONY: static clean install install-static
