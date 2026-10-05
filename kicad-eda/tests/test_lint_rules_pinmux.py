"""lint_rules_pinmux (F5-23): G1 pin-function lint for MCU interface pins.

The SAM D21 cases use the Adafruit Feather M0 / Arduino Zero assignments
(SAMD21G, 48-pin numbering) as the passing references; every rule has a
passing and a failing case."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import lint_netlist
import lint_rules_pinmux as pm
from lint_context import LintContext

TABLE = Path(pm.__file__).resolve().parents[1] / "data" / "pinmux" / "atsamd21.json"

# SAMD21G (TQFP-48 / QFN-48) pad numbers of the port pins the tests use
G = {"PA02": "3", "PA03": "4", "PA08": "13", "PA09": "14", "PA10": "15", "PA11": "16",
     "PA12": "21", "PA16": "25", "PA17": "26", "PA18": "27", "PA19": "28", "PA22": "31",
     "PA23": "32", "PB10": "19", "PB11": "20", "RESET": "40"}

SAMD21G = {"ref": "U1", "lib_id": "MCU_Microchip_SAMD:ATSAMD21G18A-A",
           "value": "ATSAMD21G18A-AU", "footprint": "Package_QFP:TQFP-48_7x7mm_P0.5mm"}
SAMD21E = {"ref": "U1", "lib_id": "SPS_parts:MCU", "value": "ATSAMD21E18A-AU",
           "footprint": "Package_QFP:TQFP-32_7x7mm_P0.8mm"}
SAMD21J = {"ref": "U1", "lib_id": "MCU_Microchip_SAMD:ATSAMD21J18A-A", "value": "ATSAMD21J18A",
           "footprint": "Package_QFP:TQFP-64_10x10mm_P0.5mm"}
STM32 = {"ref": "U1", "lib_id": "MCU_ST_STM32F1:STM32F103C8Tx", "value": "STM32F103C8T6",
         "footprint": "Package_QFP:LQFP-48_7x7mm_P0.5mm"}
ESP32 = {"ref": "U1", "lib_id": "RF_Module:ESP32-C3-WROOM-02", "value": "ESP32-C3",
         "footprint": "RF_Module:ESP32-C3-WROOM-02"}
PEER = {"ref": "J1", "lib_id": "Connector_Generic:Conn_01x16", "value": "EXT",
        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x16_P2.54mm_Vertical"}


def _design(nets: dict[str, str], mcu: dict = SAMD21G) -> dict:
    """Each net joins one MCU pad to its own J1 pin."""
    return {"components": [dict(mcu), dict(PEER)],
            "nets": {name: [[mcu["ref"], pad], ["J1", str(i + 1)]]
                     for i, (name, pad) in enumerate(nets.items())},
            "no_connect": []}


def _warnings(design: dict) -> list[str]:
    errors, warnings = pm.check(design, LintContext())
    assert errors == []
    return warnings


def _only(design: dict) -> str:
    warnings = _warnings(design)
    assert len(warnings) == 1, warnings
    return warnings[0]


# --- module contract and the table ------------------------------------------------

def test_registered_with_lint_netlist_at_order_20():
    assert pm.ORDER == 20
    rule = {r.name: r for r in lint_netlist.rule_modules()}["lint_rules_pinmux"]
    assert (rule.order, rule.problem) == (20, "") and rule.check is pm.check


def test_table_cites_the_datasheet_and_matches_the_package_pinouts():
    table = json.loads(TABLE.read_text(encoding="utf-8"))
    assert "Table 7-1" in table["source"]
    pins = table["pins"]
    for letter, count in (("E", 26), ("G", 38), ("J", 52)):
        numbers = [str(p["pin"][letter]) for p in pins.values() if p["pin"][letter]]
        other = table["packages"][letter]["other_pins"]
        assert len(numbers) == count and len(set(numbers)) == count, letter
        assert not set(numbers) & set(other), letter
    assert pins["PA10"] == {"pin": {"E": 13, "G": 15, "J": 19}, "extint": 10,
                            "sercom": [0, 2], "sercom_alt": [2, 2]}
    assert pins["PA22"]["sercom"] == [3, 0] and pins["PA23"]["sercom"] == [3, 1]
    assert pins["PA02"]["sercom"] is None and pins["PA02"]["sercom_alt"] is None
    assert pins["PA08"]["extint"] == "NMI"
    assert pins["PB10"]["sercom_alt"] == [4, 2] and pins["PB10"]["pin"]["J"] == 23


@pytest.mark.parametrize("name, parsed", [
    ("GNSS_TXD", ("TX", ("GNSS",))),
    ("UART_RX", ("RX", ("UART",))),
    ("SPI1_MOSI", ("MOSI", ("SPI", "1"))),
    ("I2C_SDA", ("SDA", ("I", "2", "C"))),
    ("/sens/1PPS", ("EXTINT", ("SENS", "1"))),
    ("IMU-IRQ", ("EXTINT", ("IMU",))),
    ("FLASH_CS", ("SS", ("FLASH",))),
    ("TX_LED", None), ("RF_EN", None), ("I2S_SCK", None),
    ("MCU_TX_GPS_RX", None), ("VSS", None), ("PA10", None), ("", None),
])
def test_net_role_tokenisation(name, parsed):
    assert pm.net_role(name) == parsed


# --- UART -----------------------------------------------------------------------

def test_feather_m0_serial1_and_i2c_pass():
    assert _warnings(_design({"UART_TX": G["PA10"], "UART_RX": G["PA11"],
                              "I2C_SDA": G["PA22"], "I2C_SCL": G["PA23"]})) == []


def test_uart_named_from_the_peer_end_passes():
    # GNSS_TXD is the module's TX: it lands on the MCU's RX pad (PA11, PAD3)
    assert _warnings(_design({"GNSS_TXD": G["PA11"], "GNSS_RXD": G["PA10"]})) == []
    # names carry no direction, so a pair is read from either end (a deliberate widening of
    # 'TX on PAD0/PAD2'): UART_RX on PAD0 can be the MCU's TX, UART_TX on PAD1 its RX
    assert _warnings(_design({"UART_TX": G["PA09"], "UART_RX": G["PA08"]})) == []


def test_uart_tx_on_a_pin_without_sercom_warns():
    w = _only(_design({"UART_TX": G["PA02"], "UART_RX": G["PA11"]}))
    assert w.startswith("PM-W1 U1.3 (PA02) on net UART_TX cannot be UART TX: candidates ")
    assert "PA10 (15)" in w and "PA08 (13)" in w and "PA14 (23)" in w
    assert "PA16" not in w and "PA11" not in w


def test_uart_pair_with_no_line_on_pad0_or_pad2_warns():
    w = _only(_design({"TX": G["PA11"], "RX": G["PA09"]}))        # PAD3 / PAD1
    assert w.startswith("PM-W1 U1.16 (PA11) on net TX cannot be UART TX: candidates ")
    assert "PA10 (15)" in w


def test_uart_pair_on_different_sercoms_warns_the_rx():
    w = _only(_design({"TX": G["PA10"], "RX": G["PA17"]}))        # SERCOM0/2 vs 1/3
    assert w.startswith("PM-W1 U1.26 (PA17) on net RX cannot be UART RX: candidates ")
    assert "PA11 (16)" in w


def test_lone_uart_line_needs_a_sercom_pad():
    assert _warnings(_design({"DBG_TX": G["PA11"]})) == []         # either end's TX
    assert _only(_design({"DBG_TX": G["PA02"]})).startswith(
        "PM-W1 U1.3 (PA02) on net DBG_TX cannot be UART TX")


def test_candidates_skip_pins_that_carry_other_nets():
    w = _only(_design({"UART_TX": G["PA02"], "UART_RX": G["PA11"], "LED": G["PA10"]}))
    assert "PA10" not in w and "PA08 (13)" in w


# --- I2C ------------------------------------------------------------------------

def test_i2c_on_sercom3_pad0_pad1_passes():
    assert _warnings(_design({"SDA": G["PA22"], "SCL": G["PA23"]})) == []


def test_i2c_swapped_pins_warn_both():
    warnings = _warnings(_design({"SDA": G["PA23"], "SCL": G["PA22"]}))
    assert [w.split(":")[0] for w in warnings] == [
        "PM-W1 U1.32 (PA23) on net SDA cannot be I2C SDA",
        "PM-W1 U1.31 (PA22) on net SCL cannot be I2C SCL"]
    assert "PA22 (31)" in warnings[0] and "PA23 (32)" in warnings[1]


def test_i2c_lines_on_two_sercoms_warn_the_scl():
    w = _only(_design({"SDA": G["PA22"], "SCL": G["PA09"]}))
    assert w.startswith("PM-W1 U1.14 (PA09) on net SCL cannot be I2C SCL: candidates ")
    assert "PA23 (32)" in w and "PA17 (26)" in w


# --- SPI ------------------------------------------------------------------------

def test_arduino_zero_spi_on_sercom4_passes():
    # MISO PA12 PAD0, MOSI PB10 PAD2, SCK PB11 PAD3 (DOPO 1, DIPO 0); CS is any GPIO
    assert _warnings(_design({"SPI_MISO": G["PA12"], "SPI_MOSI": G["PB10"],
                              "SPI_SCK": G["PB11"], "SPI_CS": G["PA02"]})) == []


def test_two_spi_buses_are_checked_separately():
    assert _warnings(_design({"SPI1_MISO": G["PA12"], "SPI1_MOSI": G["PB10"],
                              "SPI1_SCK": G["PB11"], "SPI2_MOSI": G["PA16"],
                              "SPI2_SCK": G["PA17"], "SPI2_MISO": G["PA19"]})) == []


def test_spi_sck_without_sercom_warns():
    w = _only(_design({"SPI_MISO": G["PA12"], "SPI_MOSI": G["PB10"], "SPI_SCK": G["PA02"]}))
    assert w.startswith("PM-W1 U1.3 (PA02) on net SPI_SCK cannot be SPI SCK: candidates ")
    assert "PB11 (20)" in w and "PA15 (24)" in w


def test_spi_illegal_dopo_warns_the_sck():
    # MOSI and SCK swapped: SCK on PAD2 is no DOPO's clock pad
    w = _only(_design({"MISO": G["PA12"], "MOSI": G["PB11"], "SCK": G["PB10"]}))
    assert w.startswith("PM-W1 U1.19 (PB10) on net SCK cannot be SPI SCK: candidates ")
    assert "PA13 (22)" in w and "PB09 (8)" in w


def test_spi_mosi_on_the_slave_ss_pad_warns():
    # MISO PAD0, SCK PAD1 is DOPO 0 for a slave, whose SS is PAD2: MOSI (DI) cannot be there,
    # and no master DOPO puts MOSI on PAD2 with SCK on PAD1
    w = _only(_design({"MISO": G["PA08"], "SCK": G["PA09"], "MOSI": G["PA10"]}))
    assert w.startswith("PM-W1 U1.15 (PA10) on net MOSI cannot be SPI MOSI: candidates ")
    assert "PA11 (16)" in w


def test_every_legal_slave_combination_is_a_master_combination():
    # slave: MISO is DO, MOSI is DI on a pad other than DO, SCK and the DOPO's slave SS
    slave = [{"MISO": do, "SCK": sck, "MOSI": di} for do, sck, ss in pm.SPI_DOPO
             for di in range(4) if di not in (do, sck, ss)]
    legal, advice = pm.BUS_COMBOS["SPI"]
    assert len(slave) == 4 and all(c in legal for c in slave)
    assert legal == advice and len(legal) == 8


def test_samd21j_numbering_spi_on_portb():
    pads = {"MOSI": "25", "SCK": "26", "MISO": "28"}               # PB12/PB13/PB15, SERCOM4
    assert _warnings(_design(pads, SAMD21J)) == []
    w = _only(_design({**pads, "SCK": "5"}, SAMD21J))              # PB04: no SERCOM
    assert w.startswith("PM-W1 U1.5 (PB04) on net SCK cannot be SPI SCK")


# --- PPS / interrupts ---------------------------------------------------------------

def test_pps_and_int_on_distinct_extints_pass():
    assert _warnings(_design({"GNSS_PPS": G["PA18"], "IMU_INT": G["PA19"]})) == []


def test_two_pps_inputs_on_one_extint_number_warn():
    w = _only(_design({"GNSS1_PPS": G["PA18"], "GNSS2_PPS": G["PA02"]}))   # both EXTINT[2]
    assert w.startswith("PM-W1 U1.3 (PA02) on net GNSS2_PPS cannot be PPS/INT on EXTINT[2] "
                        "(already used by net GNSS1_PPS): candidates ")
    assert "PA03 (4)" in w and "PA18" not in w and "PB02" not in w


def test_interrupt_on_the_nmi_pin_warns():
    w = _only(_design({"IRQ": G["PA08"]}))
    assert w.startswith("PM-W1 U1.13 (PA08) on net IRQ cannot be PPS/INT (needs an EXTINT "
                        "line): candidates ")


def test_interrupt_candidates_list_pins_without_sercom_first_and_are_capped():
    w = _only(_design({"IRQ": G["PA08"]}))
    listed = w.split(": candidates ")[1].split(", ")
    # PA02 PA03 PA27 PA28 have no SERCOM; the remaining 33 EXTINT pins are counted
    assert listed[:4] == ["PA02 (3)", "PA03 (4)", "PA27 (39)", "PA28 (41)"]
    assert len(listed) == pm.MAX_CANDIDATES + 1
    assert listed[-1] == f"... (+{37 - pm.MAX_CANDIDATES} more)"


def test_interface_net_on_a_power_pin_warns():
    w = _only(_design({"INT": G["RESET"]}))
    assert w.startswith("PM-W1 U1.40 (RESET) on net INT cannot be PPS/INT")


# --- which parts are checked ----------------------------------------------------------

def test_value_names_the_mcu_and_its_package():
    assert _warnings(_design({"TX": "13", "RX": "14"}, SAMD21E)) == []      # PA10 / PA11
    w = _only(_design({"TX": "3", "RX": "14"}, SAMD21E))
    assert w.startswith("PM-W1 U1.3 (PA02) on net TX cannot be UART TX") and "PA10 (13)" in w


def test_an_18_part_named_without_its_variant_letter_is_the_a_variant():
    # an Eagle import of the Feather M0: lib 'ATSAMD21J_QFN' names no part, the value does
    imported = {"ref": "U$4", "lib_id": "feather_m0_rfm_imported:ATSAMD21J_QFN",
                "value": "ATSAMD21G18_QFN", "footprint": "feather_m0_rfm_imported:QFN48"}
    feather = {"D3_IRQ": G["PA09"], "D8_CS": "11", "MISO": G["PA12"], "MOSI": G["PB10"],
               "SCK": G["PB11"], "SCL": G["PA23"], "SDA": G["PA22"]}
    assert _warnings(_design(feather, imported)) == []
    w = _only(_design({**feather, "SDA": G["PA02"]}, imported))
    assert w.startswith("PM-W1 U$4.3 (PA02) on net SDA cannot be I2C SDA")
    for value in ("ATSAMD21G17_QFN", "ATSAMD21G18B", "ATSAMD21G180"):      # not an 18A
        assert _warnings(_design(feather, {**imported, "value": value})) == [], value


def test_non_interface_names_are_not_checked():
    assert _warnings(_design({"TX_LED": G["PA02"], "I2S_SCK": G["PA03"], "RF_EN": G["PA08"],
                              "USB_DM": G["PA16"], "PA17": G["PA17"]})) == []


def test_pad_outside_the_table_gives_one_pm_i0():
    assert _warnings(_design({"TX": "B1", "RX": "C1"})) == [
        "PM-I0 pin functions not verified for MCU_Microchip_SAMD:ATSAMD21G18A-A "
        "(U1: pads B1, C1 not in the atsamd21 pin table)"]


def test_mcu_without_a_table_gets_one_pm_i0():
    assert _warnings(_design({"USART1_TX": "30", "USART1_RX": "31"}, STM32)) == [
        "PM-I0 pin functions not verified for MCU_ST_STM32F1:STM32F103C8Tx (U1)"]
    assert _warnings(_design({"I2C_SDA": "5"}, ESP32)) == [
        "PM-I0 pin functions not verified for RF_Module:ESP32-C3-WROOM-02 (U1)"]
    assert _warnings(_design({"LED": "30", "NRST": "7"}, STM32)) == []


def test_parts_sharing_a_lib_id_share_one_pm_i0():
    d = _design({"TX": "30"}, STM32)
    d["components"].append({**STM32, "ref": "U2"})
    d["nets"]["U2_RX"] = [["U2", "31"], ["J1", "9"]]
    assert _warnings(d) == [
        "PM-I0 pin functions not verified for MCU_ST_STM32F1:STM32F103C8Tx (U1, U2)"]


@pytest.mark.parametrize("lib_id, value", [
    ("RF_Module:RFM95W-915S2", "RFM95W"), ("RF_Module:RFM69HCW", "RFM69HCW"),
    ("RF_Module:Ai-Thinker-Ra-01", "Ra-01"), ("RF_Module:DWM1000", "DWM1000"),
    ("RF_Module:DWM3000", "DWM3000"),
])
def test_fixed_function_radio_modules_get_no_pm_i0(lib_id, value):
    radio = {"ref": "U2", "lib_id": lib_id, "value": value, "footprint": f"RF_Module:{value}"}
    assert _warnings(_design({"LORA_MOSI": "4", "LORA_SCK": "5", "LORA_IRQ": "6"}, radio)) == []


def test_programmable_rf_modules_get_pm_i0():
    module = {"ref": "U3", "lib_id": "RF_Module:DWM1001", "value": "DWM1001",
              "footprint": "RF_Module:DWM1001"}                    # nRF52832 inside
    assert _warnings(_design({"UWB_TX": "4"}, module)) == [
        "PM-I0 pin functions not verified for RF_Module:DWM1001 (U3)"]


def test_non_mcu_parts_are_not_checked():
    uart_bridge = {"ref": "U1", "lib_id": "Interface_USB:CP2102N-A02-GQFN28",
                   "value": "CP2102N", "footprint": "Package_DFN_QFN:QFN-28"}
    assert _warnings(_design({"TXD": "26", "RXD": "25"}, uart_bridge)) == []


# --- robustness and integration -----------------------------------------------------

@pytest.mark.parametrize("design", [
    {}, {"components": None, "nets": 7},
    {"components": [None, {"ref": "U1"}, SAMD21G], "nets": {"TX": [["U1"], "x", None, 5]}},
    {"components": [SAMD21G], "nets": {"TX": None, "RX": [["U1", 16]]}},
])
def test_malformed_sections_never_raise(design):
    assert pm.check(design, LintContext()) == ([], [])


def test_check_does_not_change_the_design():
    d = _design({"UART_TX": G["PA02"], "UART_RX": G["PA11"]})
    before = copy.deepcopy(d)
    pm.check(d, LintContext())
    assert d == before


def test_lint_carries_pinmux_findings_as_warnings():
    rep = lint_netlist.lint(_design({"UART_TX": G["PA02"], "UART_RX": G["PA11"]}))
    # warnings only: a pin-function finding never fails lint (other modules' rules aside)
    assert [e for e in rep["errors"] if "pinmux" in e or e.startswith("PM-")] == []
    assert [w for w in rep["warnings"] if w.startswith("PM-")] == [
        _only(_design({"UART_TX": G["PA02"], "UART_RX": G["PA11"]}))]


def test_example_netlists_get_no_pinmux_errors_or_misplaced_pins(example_netlists):
    # Only pinmux findings are asserted: other rule modules own the rest of lint, and an example
    # with an untabled MCU (a later one built from esp32c3_env_node, say) may carry a PM-I0.
    found = {}
    for path in example_netlists:
        design = json.loads(path.read_text(encoding="utf-8"))
        errors, warnings = pm.check(design, LintContext())
        assert errors == [], path.parent.name
        assert [w for w in warnings if not w.startswith("PM-I0 ")] == [], path.parent.name
        mine = [e for e in lint_netlist.lint(design)["errors"]
                if e.startswith(("PM-", "LINT-INTERNAL lint_rules_pinmux"))]
        assert mine == [], path.parent.name
        found[path.parent.name] = warnings
    assert found["esp32c3_env_node"] == [
        "PM-I0 pin functions not verified for RF_Module:ESP32-C3-WROOM-02 (U1)"]
