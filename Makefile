PACKAGE  := caddy-tui
PREFIX   ?= /usr
DESTDIR  ?=
LANGS    := $(patsubst po/%.po,%,$(wildcard po/*.po))

.PHONY: all pot update-po mo install test clean

all: mo

# Extract translatable strings (needs gettext's xgettext).
pot:
	xgettext -L Python --from-code=UTF-8 --no-location -k_ -kN_ --package-name=$(PACKAGE) \
	  --msgid-bugs-address=mgoreiro@gmail.com --copyright-holder="Miguel González Oreiro" \
	  -o po/$(PACKAGE).pot caddy-tui.py

update-po: pot
	for l in $(LANGS); do msgmerge -q -U --no-fuzzy-matching po/$$l.po po/$(PACKAGE).pot; done

# Compile catalogs next to the script (used when running from a checkout).
mo:
	for l in $(LANGS); do \
	  mkdir -p locale/$$l/LC_MESSAGES; \
	  msgfmt --check -o locale/$$l/LC_MESSAGES/$(PACKAGE).mo po/$$l.po; \
	done

install: mo
	install -D -m 0755 caddy-tui.py $(DESTDIR)$(PREFIX)/bin/$(PACKAGE)
	for l in $(LANGS); do \
	  install -D -m 0644 locale/$$l/LC_MESSAGES/$(PACKAGE).mo \
	    $(DESTDIR)$(PREFIX)/share/locale/$$l/LC_MESSAGES/$(PACKAGE).mo; \
	done
	install -D -m 0644 man/$(PACKAGE).1 $(DESTDIR)$(PREFIX)/share/man/man1/$(PACKAGE).1

test: mo
	python3 -m unittest discover -s tests -v

clean:
	rm -rf locale
