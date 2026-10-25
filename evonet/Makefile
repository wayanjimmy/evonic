# Evonet Makefile

BINARY  := evonet
GO      := go
GOFLAGS := -trimpath -ldflags="-s -w"
# GUI Windows build hides the console window (no terminal popup on double-click)
GOFLAGS_WIN_GUI := -trimpath -ldflags="-s -w -H windowsgui"
DIST    := dist
ZIP     := zip
DITTO   := ditto

RELEASE_NAME          := evonet
ARCHIVE_LINUX_AMD64   := $(DIST)/$(RELEASE_NAME)-linux-amd64.zip
ARCHIVE_LINUX_ARM64   := $(DIST)/$(RELEASE_NAME)-linux-arm64.zip
ARCHIVE_MACOS_ARM64   := $(DIST)/$(RELEASE_NAME)-macos-arm64.zip
ARCHIVE_MACOS_AMD64   := $(DIST)/$(RELEASE_NAME)-macos-amd64.zip
ARCHIVE_WINDOWS_AMD64 := $(DIST)/$(RELEASE_NAME)-windows-amd64.zip
ARCHIVE_MACOS_APP     := $(DIST)/$(RELEASE_NAME)-macos-arm64-app.zip
ARCHIVES := $(ARCHIVE_LINUX_AMD64) $(ARCHIVE_LINUX_ARM64) \
            $(ARCHIVE_MACOS_ARM64) $(ARCHIVE_MACOS_AMD64) \
            $(ARCHIVE_WINDOWS_AMD64)

## Per-platform archive targets that make up a CLI release.
RELEASE_CLI_TARGETS := release-linux-amd64 release-linux-arm64 \
                       release-macos-arm64 release-macos-amd64 \
                       release-windows-amd64

## Hash every archive that exists in dist/ (recursive "=" so $(wildcard) is
## evaluated when the recipe runs, after the archives have been produced).
CHECKSUMS = cd $(DIST) && shasum -a 256 $(notdir $(wildcard $(ARCHIVES) $(ARCHIVE_MACOS_APP))) > checksums.txt

.PHONY: build build-linux build-linux-arm64 build-windows build-macos build-all \
        build-gui-windows build-gui-macos clean embed test \
        release release-all release-linux-amd64 release-linux-arm64 \
        release-macos release-macos-arm64 release-macos-amd64 \
        release-windows-amd64 release-gui-macos release-checksums

## Build for current platform (output: dist/evonet)
build:
	mkdir -p $(DIST)
	$(GO) build $(GOFLAGS) -o $(DIST)/$(BINARY) .

## Build CLI binary for Linux (amd64) — full GUI included (Fyne + CGo work natively on Linux).
build-linux:
	mkdir -p $(DIST)
	GOOS=linux GOARCH=amd64 $(GO) build $(GOFLAGS) -o $(DIST)/$(BINARY)-linux-amd64 .

## Build CLI binary for Linux (arm64) — headless / no GUI.
## Uses -tags headless + CGO_ENABLED=0 because on arm64 the Fyne GUI is excluded
## (build-tag constraint: (windows || darwin) && !headless), so only the stub is compiled.
build-linux-arm64:
	mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build $(GOFLAGS) -tags headless -o $(DIST)/$(BINARY)-linux-arm64 .

## Build CLI binary for macOS (arm64 + amd64) — headless / no GUI.
## Uses -tags headless so it compiles on Linux without macOS CGo/OpenGL headers.
## For a real GUI .app build, run build-gui-macos on a Mac.
build-macos:
	mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 $(GO) build $(GOFLAGS) -tags headless -o $(DIST)/$(BINARY)-darwin-arm64 .
	CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 $(GO) build $(GOFLAGS) -tags headless -o $(DIST)/$(BINARY)-darwin-amd64 .

## Build CLI binary for Windows (amd64) — headless / no GUI.
## Uses -tags headless so it compiles on Linux without mingw-w64 CGo toolchain.
## For a GUI binary (with hidden console), use build-gui-windows (fyne-cross) or
## build-gui-windows-native (requires mingw-w64).
build-windows:
	mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=windows GOARCH=amd64 $(GO) build $(GOFLAGS) -tags headless -o $(DIST)/$(BINARY)-windows-amd64.exe .

## Build all platforms into dist/
build-all: build-linux build-linux-arm64 build-macos build-windows

## Build GUI binary for Windows (hides console window; requires fyne-cross + Docker)
## Install fyne-cross: go install fyne.io/fyne/v2/cmd/fyne-cross@latest
build-gui-windows:
	mkdir -p $(DIST)
	fyne-cross windows -arch amd64 -output $(BINARY)-windows-amd64.exe .
	mv fyne-cross/dist/windows-amd64/$(BINARY)-windows-amd64.exe.zip $(DIST)/ 2>/dev/null || true

## Build GUI binary for Windows without Docker (cross-compile from Linux, requires mingw-w64)
build-gui-windows-native:
	mkdir -p $(DIST)
	CGO_ENABLED=1 CC=x86_64-w64-mingw32-gcc \
	GOOS=windows GOARCH=amd64 \
	$(GO) build $(GOFLAGS_WIN_GUI) -o $(DIST)/$(BINARY)-windows-amd64.exe .

## Build GUI .app bundle for macOS — run ON a Mac (requires Xcode CLI tools + fyne)
## go install fyne.io/fyne/v2/cmd/fyne@latest
FYNE := $(shell $(GO) env GOPATH)/bin/fyne
build-gui-macos:
	mkdir -p $(DIST)
	rm -rf $(DIST)/Evonet.app
	$(FYNE) package --target darwin --name Evonet --app-id com.evonic.evonet --icon Icon.png 2>/dev/null || \
	$(FYNE) package --target darwin --name Evonet --app-id com.evonic.evonet
	mv Evonet.app $(DIST)/Evonet.app
	@echo "Built: $(DIST)/Evonet.app"

## Embed a config.json into a binary: make embed BINARY=dist/evonet-linux-amd64 CONFIG=myconfig.json
embed:
	./scripts/embed_config.sh $(BINARY) $(CONFIG)

## Run tests
test:
	$(GO) test ./...

clean:
	rm -rf $(DIST)

## Build ZIP assets for every CLI platform and write SHA-256 checksums.
release: $(RELEASE_CLI_TARGETS) release-checksums
	@echo "Built CLI release archives and $(DIST)/checksums.txt"

## Build every release asset, including the macOS GUI app bundle (macOS only).
release-all: release release-gui-macos

## Build and package the Linux amd64 CLI distribution.
release-linux-amd64:
	$(MAKE) build-linux
	@cd $(DIST) && $(ZIP) -q -9 -FS $(notdir $(ARCHIVE_LINUX_AMD64)) $(BINARY)-linux-amd64

## Build and package the Linux arm64 CLI distribution.
release-linux-arm64:
	$(MAKE) build-linux-arm64
	@cd $(DIST) && $(ZIP) -q -9 -FS $(notdir $(ARCHIVE_LINUX_ARM64)) $(BINARY)-linux-arm64

## Build and package both macOS CLI distributions.
release-macos: release-macos-arm64 release-macos-amd64

## Build and package the macOS Apple Silicon CLI distribution.
release-macos-arm64:
	$(MAKE) build-macos
	@cd $(DIST) && $(ZIP) -q -9 -FS $(notdir $(ARCHIVE_MACOS_ARM64)) $(BINARY)-darwin-arm64

## Build and package the macOS Intel CLI distribution.
release-macos-amd64:
	$(MAKE) build-macos
	@cd $(DIST) && $(ZIP) -q -9 -FS $(notdir $(ARCHIVE_MACOS_AMD64)) $(BINARY)-darwin-amd64

## Build and package the Windows amd64 CLI distribution.
release-windows-amd64:
	$(MAKE) build-windows
	@cd $(DIST) && $(ZIP) -q -9 -FS $(notdir $(ARCHIVE_WINDOWS_AMD64)) $(BINARY)-windows-amd64.exe

## Build and package the macOS Apple Silicon GUI app bundle (must run on macOS).
release-gui-macos:
	$(MAKE) build-gui-macos
	@$(DITTO) -c -k --sequesterRsrc --keepParent $(DIST)/Evonet.app $(ARCHIVE_MACOS_APP)
	@echo "Built: $(ARCHIVE_MACOS_APP)"
	@[ -f $(DIST)/checksums.txt ] && $(CHECKSUMS) && echo "Refreshed: $(DIST)/checksums.txt" || true

## Write SHA-256 checksums for every archive currently present in dist/.
release-checksums: $(RELEASE_CLI_TARGETS)
	@$(CHECKSUMS)
	@echo "Built: $(DIST)/checksums.txt"
