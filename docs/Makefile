# Build the API reference. `make html` writes docs/build; `make strict` fails on
# any warning, which is what CI should run.

SPHINXBUILD = uv run sphinx-build
SOURCE      = source
BUILD       = build

.PHONY: help html strict clean open

help:
	@echo "html    build the HTML reference into $(BUILD)"
	@echo "strict  same, but any warning fails the build"
	@echo "clean   remove the build output"
	@echo "open    build, then open the entry page"

html:
	$(SPHINXBUILD) -b html $(SOURCE) $(BUILD)

strict:
	$(SPHINXBUILD) -b html $(SOURCE) $(BUILD) -W --keep-going

clean:
	rm -rf $(BUILD)

open: html
	@xdg-open $(BUILD)/index.html 2>/dev/null || echo "open $(BUILD)/index.html"
