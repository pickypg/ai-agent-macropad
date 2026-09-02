# Firmware builds for QMK pads. Output lands in qmk-userspace/ as
#   <board>_ai_agent_macropad-p<protocol>-qmk_<hash>[-dirty]-overlay_<hash>[-dirty].bin
# which is gitignored. Hash the QMK fork and this overlay so someone
# else can rebuild the same image; the protocol version is also on the
# wire (see hid_protocol.PROTOCOL_VERSION).
#
#   make nuphy-air75-v2
#   make keychron-k1-pro
#   make keychron-k0-max
#   NUPHY_QMK=/other/nuphy-qmk-firmware make nuphy-air75-v2
#   KEYCHRON_QMK=/other/keychron-qmk-firmware make keychron-k1-pro
#   KEYCHRON_MAX_QMK=/other/keychron-qmk-firmware-2025q3 make keychron-k0-max
#
# This repo does not flash. On the K0 Max (STM32L432) use dfu-util, not
# the browser flasher — see README "QMK keyboard (Keychron K0 Max)":
#   dfu-util -a 0 -d 0483:df11 -s 0x08000000:leave -t 1024 -D qmk-userspace/<bin>
# Other boards: qmk-browser-flasher or `qmk flash` against the sibling
# QMK checkout.
#
# Do not set TARGET in keymap rules.mk — QMK freezes INTERMEDIATE_OUTPUT
# from TARGET before those files are included. -e TARGET=... is the
# supported hook (TARGET ?= in build_keyboard.mk).

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
QMK_USERSPACE := $(ROOT)/qmk-userspace
NUPHY_QMK ?= $(abspath $(ROOT)/../nuphy-qmk-firmware)
KEYCHRON_QMK ?= $(abspath $(ROOT)/../keychron-qmk-firmware)
KEYCHRON_MAX_QMK ?= $(abspath $(ROOT)/../keychron-qmk-firmware-2025q3)

PROTOCOL_VERSION := $(shell python3 -c 'import sys; sys.path.insert(0, "$(ROOT)"); from hid_protocol import PROTOCOL_VERSION; print(PROTOCOL_VERSION)')

# Short git hash for $1, with a -dirty suffix if that tree has staged
# or unstaged changes. "unknown" if $1 isn't a git checkout.
git_stamp = $(shell \
	if git -C $(1) rev-parse --is-inside-work-tree >/dev/null 2>&1; then \
		h=$$(git -C $(1) rev-parse --short HEAD); \
		if ! git -C $(1) diff --quiet --exit-code || ! git -C $(1) diff --cached --quiet --exit-code; then \
			h=$${h}-dirty; \
		fi; \
		printf '%s' $$h; \
	else \
		printf 'unknown'; \
	fi)

OVERLAY_HASH := $(call git_stamp,$(ROOT))
NUPHY_QMK_HASH := $(call git_stamp,$(NUPHY_QMK))
KEYCHRON_QMK_HASH := $(call git_stamp,$(KEYCHRON_QMK))
KEYCHRON_MAX_QMK_HASH := $(call git_stamp,$(KEYCHRON_MAX_QMK))

NUPHY_TARGET := nuphy_air75_v2_ansi_ai_agent_macropad-p$(PROTOCOL_VERSION)-qmk_$(NUPHY_QMK_HASH)-overlay_$(OVERLAY_HASH)
KEYCHRON_TARGET := keychron_k1_pro_ansi_rgb_ai_agent_macropad-p$(PROTOCOL_VERSION)-qmk_$(KEYCHRON_QMK_HASH)-overlay_$(OVERLAY_HASH)
KEYCHRON_MAX_TARGET := keychron_k0_max_ai_agent_macropad-p$(PROTOCOL_VERSION)-qmk_$(KEYCHRON_MAX_QMK_HASH)-overlay_$(OVERLAY_HASH)

.PHONY: all nuphy-air75-v2 keychron-k1-pro keychron-k0-max

all: nuphy-air75-v2

nuphy-air75-v2:
	QMK_HOME="$(NUPHY_QMK)" QMK_USERSPACE="$(QMK_USERSPACE)" qmk compile \
		-kb nuphy/air75_v2/ansi -km ai_agent_macropad \
		-e TARGET="$(NUPHY_TARGET)"

keychron-k1-pro:
	QMK_HOME="$(KEYCHRON_QMK)" QMK_USERSPACE="$(QMK_USERSPACE)" qmk compile \
		-kb keychron/k1_pro/ansi/rgb -km ai_agent_macropad \
		-e TARGET="$(KEYCHRON_TARGET)"

keychron-k0-max:
	QMK_HOME="$(KEYCHRON_MAX_QMK)" QMK_USERSPACE="$(QMK_USERSPACE)" qmk compile \
		-kb keychron/k0_max -km ai_agent_macropad \
		-e TARGET="$(KEYCHRON_MAX_TARGET)"
