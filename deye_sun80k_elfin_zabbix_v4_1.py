#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Deye SUN-80K-SG02HP3-EU-EM6 -> Elfin EE11A -> Zabbix (v4 production)
======================================================

Connection:
    Zabbix server --Ethernet/Modbus TCP--> Elfin EE11A
    Elfin EE11A --RS485/Modbus RTU--> Deye SUN-80K

READ ONLY: uses Modbus function 03 (Read Holding Registers).
No commands/settings are written to the inverter.

Elfin must have "Modbus" conversion enabled (Modbus TCP <-> Modbus RTU).
"""

import argparse
import datetime
import json
import socket
import struct
import subprocess
import sys
import time


# ============================================================
# CONFIG
# ============================================================

ELFIN_IP = "192.168.31.100"     # <-- CHANGE to Elfin EE11A IP
ELFIN_PORT = 8899               # usually 8899
MODBUS_SLAVE_ID = 0             # confirmed working on this SUN-80K

SOCKET_TIMEOUT = 3.0
POLL_SEC = 30

ZABBIX_SERVER = "127.0.0.1"
ZABBIX_HOST = "Deye_80k"
ZABBIX_SENDER = "/usr/bin/zabbix_sender"

# ============================================================


class ModbusError(Exception):
    pass


class ModbusTCP:
    """Minimal Modbus TCP client implementing FC03 only."""

    def __init__(self, host, port=502, unit=1, timeout=3.0):
        self.host = host
        self.port = int(port)
        self.unit = int(unit)
        self.timeout = float(timeout)
        self.sock = None
        self.tid = 0

    def connect(self):
        self.close()
        self.sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        )
        self.sock.settimeout(self.timeout)

    def close(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def _recv_exact(self, n):
        data = bytearray()
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ModbusError("TCP connection closed")
            data.extend(chunk)
        return bytes(data)

    def read_holding_registers(self, address, quantity):
        if not (1 <= quantity <= 125):
            raise ValueError("quantity must be 1..125")

        if self.sock is None:
            self.connect()

        self.tid = (self.tid + 1) & 0xFFFF

        # PDU: FC03 + start addr + quantity
        pdu = struct.pack(">BHH", 0x03, address, quantity)

        # MBAP: transaction, protocol=0, length=(unit + PDU), unit
        mbap = struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit)

        try:
            self.sock.sendall(mbap + pdu)

            hdr = self._recv_exact(7)
            r_tid, proto, length, unit = struct.unpack(">HHHB", hdr)

            if proto != 0:
                raise ModbusError(f"Bad protocol ID: {proto}")
            if r_tid != self.tid:
                raise ModbusError(
                    f"Transaction mismatch: sent={self.tid}, received={r_tid}"
                )

            body = self._recv_exact(length - 1)

            if not body:
                raise ModbusError("Empty Modbus response")

            fc = body[0]

            if fc & 0x80:
                exc = body[1] if len(body) > 1 else -1
                raise ModbusError(
                    f"Modbus exception: FC=0x{fc:02X}, exception={exc}"
                )

            if fc != 0x03:
                raise ModbusError(f"Unexpected function: 0x{fc:02X}")

            byte_count = body[1]
            raw = body[2:]

            if len(raw) != byte_count:
                raise ModbusError(
                    f"Bad byte count: declared={byte_count}, got={len(raw)}"
                )

            if byte_count != quantity * 2:
                raise ModbusError(
                    f"Expected {quantity*2} data bytes, got {byte_count}"
                )

            return list(struct.unpack(">" + "H" * quantity, raw))

        except (socket.timeout, OSError, ModbusError):
            # force reconnect on next request
            self.close()
            raise


def s16(v):
    return v - 65536 if v >= 32768 else v


def u32_lh(lo, hi):
    return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)


def read_u16(mb, addr):
    return mb.read_holding_registers(addr, 1)[0]


def read_i16(mb, addr):
    return s16(read_u16(mb, addr))


def read_u32_lh(mb, addr):
    r = mb.read_holding_registers(addr, 2)
    return u32_lh(r[0], r[1])


def safe_read(func, default=None):
    try:
        return func()
    except Exception:
        return default


def scale(value, factor, digits=2):
    if value is None:
        return None
    return round(value * factor, digits)


def temp_deye(value):
    if value is None:
        return None
    return round((value - 1000) * 0.1, 1)


def bit_numbers(value):
    if not value:
        return []
    return [i for i in range(16) if value & (1 << i)]


RUN_STATE = {
    0: "Standby",
    1: "Self-check",
    2: "Normal",
    3: "Alarm",
    4: "Fault",
}


# Deye SUN-(60-80)K-SG02HP3-EU-EM6 alarm code names.
# Warning bit layout:
#   reg 553 bits 0..15 -> W01..W16
#   reg 554 bits 0..15 -> W17..W32
# Fault bit layout:
#   reg 555 bits 0..15 -> F01..F16
#   reg 556 bits 0..15 -> F17..F32
#   reg 557 bits 0..15 -> F33..F48
#   reg 558 bits 0..15 -> F49..F64

WARNING_NAMES = {
    1: "Reserved",
    2: "FAN_IN_Warn",
    3: "Grid_phase_warn",
    4: "Meter_offline_warn",
    5: "CT_WRONG_direction_warn",
    6: "CT_Notconnect_warn",
    7: "FAN_OUT1_Warn",
    8: "FAN_OUT2_Warn",
    9: "FAN_OUT3_Warn",
    10: "VW_activate",
    31: "Battery_comm_warn",
    32: "Parallel_comm_warn",
}

FAULT_NAMES = {
    1: "DC_Inversed_Failure",
    2: "DC_Insulation_Failure",
    3: "GFDI_Failure",
    4: "GFDI_Ground_Failure",
    5: "EEPROM_Read_Failure",
    6: "EEPROM_Write_Failure",
    7: "DCDC1_START_Failure",
    8: "DCDC2_START_Failure",
    9: "IGBT_Failure",
    10: "AuxPowerBoard_Failure",
    11: "AC_MainContactor_Failure",
    12: "AC_SlaveContactor_Failure",
    13: "Working_Mode_Change",
    14: "DC_OverCurr_Failure",
    15: "AC_OverCurr_SW_Failure",
    16: "GFCI_Failure",
    17: "Tz_PV_OverCurr_Fault",
    18: "Tz_AC_OverCurr_Fault",
    19: "Tz_Integ_Fault",
    20: "Tz_Dc_OverCurr_Fault",
    21: "Tz_HV_Overcurr_Fault",
    22: "Tz_EmergStop_Fault",
    23: "Tz_GFCI_OC_Fault",
    24: "DC_Insulation_Fault",
    25: "DC_Feedback_Fault",
    26: "BusUnbalance_Fault",
    27: "DC_Insulation_Fault",
    28: "DCIOver_M1_Fault",
    29: "Parallel_Comm_Fault",
    30: "AC_MainContactor_Fault",
    31: "AC_SlaveContactor_Fault",
    32: "DCIOver_M2_Fault",
    33: "AC_OverCurr_Fault",
    34: "AC_Overload_Fault",
    35: "AC_NoUtility_Fault",
    36: "Reserved",
    37: "Reserved",
    38: "Reserved",
    39: "INT_AC_OverCurr_Fault",
    40: "INT_DC_OverCurr_Fault",
    41: "Parallel_system_Stop",
    42: "Parallel_Version_Fault",
    43: "Reserved",
    44: "Reserved",
    45: "AC_UV_OverVolt_Fault",
    46: "AC_UV_UnderVolt_Fault",
    47: "AC_OverFreq_Fault",
    48: "AC_UnderFreq_Fault",
    49: "AC_U_GridCurr_DcHigh_Fault",
    50: "AC_V_GridCurr_DcHigh_Fault",
    51: "Battery_Temp_High_Fault",
    52: "DC_VoltHigh_Fault",
    53: "DC_VoltLow_Fault",
    54: "BAT2_VoltHigh_Fault",
    55: "BAT1_VoltHigh_Fault",
    56: "BAT1_VoltLow_Fault",
    57: "BAT2_VoltLow_Fault",
    58: "Battery_Comm_Lose",
    59: "Reserved",
    60: "GEN_FAULT",
    61: "INVERTER_Manual_OFF",
    62: "DRMs_Stop",
    63: "ARC_Fault",
    64: "Heatsink_HighTemp_Fault",
}


WARNING_UA = {
    1: "Зарезервовано",
    2: "Попередження внутрішнього вентилятора",
    3: "Попередження про послідовність/фази мережі",
    4: "Втрачено зв'язок з лічильником",
    5: "Неправильний напрямок трансформатора струму (CT)",
    6: "Трансформатор струму (CT) не підключений",
    7: "Попередження вихідного вентилятора 1",
    8: "Попередження вихідного вентилятора 2",
    9: "Попередження вихідного вентилятора 3",
    10: "Активовано функцію VW",
    31: "Попередження зв'язку з батареєю/BMS",
    32: "Попередження паралельного зв'язку",
}

FAULT_UA = {
    1: "Зворотна полярність DC",
    2: "Помилка ізоляції DC",
    3: "Помилка GFDI",
    4: "Замикання на землю GFDI",
    5: "Помилка читання EEPROM",
    6: "Помилка запису EEPROM",
    7: "Помилка запуску DCDC1",
    8: "Помилка запуску DCDC2",
    9: "Помилка IGBT",
    10: "Помилка плати допоміжного живлення",
    11: "Помилка головного AC-контактора",
    12: "Помилка підлеглого AC-контактора",
    13: "Зміна режиму роботи",
    14: "Перевищення струму DC",
    15: "Програмна помилка перевищення струму AC",
    16: "Помилка GFCI",
    17: "Аварійне перевищення струму PV",
    18: "Аварійне перевищення струму AC",
    19: "Внутрішня інтегральна аварія",
    20: "Аварійне перевищення струму DC",
    21: "Аварійне перевищення струму високовольтної шини",
    22: "Аварійна зупинка",
    23: "Аварійне перевищення струму GFCI",
    24: "Помилка ізоляції DC",
    25: "Помилка зворотного зв'язку DC",
    26: "Дисбаланс DC-шини",
    27: "Помилка ізоляції DC",
    28: "Перевищення постійної складової струму M1",
    29: "Помилка паралельного зв'язку",
    30: "Помилка головного AC-контактора",
    31: "Помилка підлеглого AC-контактора",
    32: "Перевищення постійної складової струму M2",
    33: "Перевищення струму AC",
    34: "Перевантаження AC",
    35: "Мережа AC відсутня",
    36: "Зарезервовано",
    37: "Зарезервовано",
    38: "Зарезервовано",
    39: "Внутрішнє перевищення струму AC",
    40: "Внутрішнє перевищення струму DC",
    41: "Зупинка паралельної системи",
    42: "Несумісність версій паралельної системи",
    43: "Зарезервовано",
    44: "Зарезервовано",
    45: "Завищена напруга мережі AC",
    46: "Занижена напруга мережі AC",
    47: "Завищена частота мережі AC",
    48: "Занижена частота мережі AC",
    49: "Завелика DC-складова струму фази U мережі",
    50: "Завелика DC-складова струму фази V мережі",
    51: "Висока температура батареї",
    52: "Завищена напруга DC",
    53: "Занижена напруга DC",
    54: "Завищена напруга батареї 2",
    55: "Завищена напруга батареї 1",
    56: "Занижена напруга батареї 1",
    57: "Занижена напруга батареї 2",
    58: "Втрачено зв'язок з батареєю/BMS",
    59: "Зарезервовано",
    60: "Аварія входу генератора",
    61: "Інвертор вимкнено вручну",
    62: "Зупинка за сигналом DRM",
    63: "Дугова аварія (ARC)",
    64: "Висока температура радіатора",
}


def decode_code_words(words, prefix, names):
    """Decode consecutive 16-bit words to Wxx/Fxx codes."""
    out = []
    for word_index, value in enumerate(words):
        if value is None:
            continue
        for bit in range(16):
            if value & (1 << bit):
                number = word_index * 16 + bit + 1
                code = f"{prefix}{number:02d}"
                name = names.get(number, "Reserved/Unknown")
                out.append((code, name))
    return out


def code_list_text(decoded):
    return ",".join(code for code, _ in decoded) if decoded else "None"


def code_summary_text(decoded):
    if not decoded:
        return "Немає помилок"
    parts = []
    for code, original in decoded:
        number = int(code[1:])
        ua_map = WARNING_UA if code.startswith("W") else FAULT_UA
        ua = ua_map.get(number, "Невідомий/зарезервований код")
        parts.append(f"{code} {original} — {ua}")
    return " | ".join(parts)


def collect(mb):
    """SUN-80K-SG02HP3-EU-EM6 validated read-only telemetry (v4 production).

    Scaling/direction fixes are based on this inverter's live Modbus dump
    cross-checked with the Deye energy-flow display.
    """
    m = {"deye.comm.ok": 1}

    # Read the live telemetry area in blocks to reduce RS485 traffic.
    regs = {}
    for base, qty in ((500, 100), (600, 100), (700, 20)):
        try:
            vals = mb.read_holding_registers(base, qty)
            regs.update({base + i: v for i, v in enumerate(vals)})
        except Exception:
            # Fallback to individual reads if a block is rejected.
            for a in range(base, base + qty):
                regs[a] = safe_read(lambda a=a: read_u16(mb, a))

    def u(addr):
        return regs.get(addr)

    def i(addr):
        v = u(addr)
        return None if v is None else s16(v)

    # STATUS / TEMPERATURES
    state = u(500)
    m["deye.run_state"] = state
    m["deye.run_state_text"] = RUN_STATE.get(state, f"Unknown({state})")
    m["deye.temp.dc_c"] = temp_deye(u(540))
    m["deye.temp.ac_c"] = temp_deye(u(541))

    # WARNING / FAULT bitfields.
    # Deye Modbus map for this register family:
    #   553-554 = two warning words (W01..W32)
    #   555-558 = four fault words   (F01..F64)
    warnings = [u(a) for a in (553, 554)]
    faults = [u(a) for a in (555, 556, 557, 558)]

    for n, val in enumerate(warnings, 1):
        m[f"deye.warning{n}.raw"] = val
    for n, val in enumerate(faults, 1):
        m[f"deye.fault{n}.raw"] = val

    decoded_warnings = decode_code_words(warnings, "W", WARNING_NAMES)
    decoded_faults = decode_code_words(faults, "F", FAULT_NAMES)

    warning_reads_ok = all(v is not None for v in warnings)
    fault_reads_ok = all(v is not None for v in faults)
    alarm_reads_ok = warning_reads_ok and fault_reads_ok

    m["deye.warning.active"] = None if not warning_reads_ok else int(bool(decoded_warnings))
    m["deye.warning.codes"] = "Помилка читання Modbus" if not warning_reads_ok else code_list_text(decoded_warnings)
    m["deye.warning.summary"] = "Помилка читання Modbus" if not warning_reads_ok else code_summary_text(decoded_warnings)

    m["deye.fault.active"] = None if not fault_reads_ok else int(bool(decoded_faults))
    m["deye.fault.codes"] = "Помилка читання Modbus" if not fault_reads_ok else code_list_text(decoded_faults)
    m["deye.fault.summary"] = "Помилка читання Modbus" if not fault_reads_ok else code_summary_text(decoded_faults)

    m["deye.alarm.active"] = None if not alarm_reads_ok else int(
        bool(decoded_warnings or decoded_faults or state in (3, 4))
    )
    if not alarm_reads_ok:
        m["deye.alarm.summary"] = "Помилка читання Modbus"
    elif decoded_warnings or decoded_faults:
        parts = []
        if decoded_warnings:
            parts.append("ПОПЕРЕДЖЕННЯ / WARNING: " + code_summary_text(decoded_warnings))
        if decoded_faults:
            parts.append("АВАРІЯ / FAULT: " + code_summary_text(decoded_faults))
        m["deye.alarm.summary"] = " | ".join(parts)
    else:
        m["deye.alarm.summary"] = "Немає помилок"

    # ENERGY / PV summary (529 is validated against the 500+ HP3 family map)
    m["deye.pv.energy_today_kwh"] = scale(u(529), 0.1, 1)

    # BATTERY (HP3: high-voltage battery)
    m["deye.battery.temp_c"] = temp_deye(u(586))
    m["deye.battery.voltage_v"] = scale(u(587), 0.1, 1)
    m["deye.battery.soc_pct"] = u(588)
    # On this SG02HP3 dump these are consistent with 10 W and 0.01 A scaling.
    m["deye.battery.power_w"] = None if i(590) is None else i(590) * 10
    m["deye.battery.current_a"] = scale(i(591), 0.01, 2)
    # Confirmed against Deye energy-flow screen: negative = charging, positive = discharging.
    bp = m["deye.battery.power_w"]
    m["deye.battery.charge_w"] = None if bp is None else max(-bp, 0)
    m["deye.battery.discharge_w"] = None if bp is None else max(bp, 0)

    # GRID
    for ph, addr in zip(("l1", "l2", "l3"), (598, 599, 600)):
        m[f"deye.grid.voltage_{ph}_v"] = scale(u(addr), 0.1, 1)
    m["deye.grid.frequency_hz"] = scale(u(609), 0.01, 2)
    # Confirmed against the Deye energy-flow screen on this SG02HP3:
    # 604..606 and 625 are already in watts (NO x10 multiplier).
    for ph, addr in zip(("l1", "l2", "l3"), (604, 605, 606)):
        m[f"deye.grid.ct_power_{ph}_w"] = i(addr)
    m["deye.grid.power_w"] = i(625)
    if m["deye.grid.power_w"] is not None:
        m["deye.grid.import_w"] = max(m["deye.grid.power_w"], 0)
        m["deye.grid.export_w"] = max(-m["deye.grid.power_w"], 0)

    # INVERTER AC
    for ph, addr in zip(("l1", "l2", "l3"), (627, 628, 629)):
        m[f"deye.inverter.voltage_{ph}_v"] = scale(u(addr), 0.1, 1)
    for ph, addr in zip(("l1", "l2", "l3"), (630, 631, 632)):
        m[f"deye.inverter.current_{ph}_a"] = scale(u(addr), 0.01, 2)
    for ph, addr in zip(("l1", "l2", "l3"), (633, 634, 635)):
        m[f"deye.inverter.power_{ph}_w"] = i(addr)
    m["deye.inverter.power_total_w"] = i(636)
    m["deye.inverter.frequency_hz"] = scale(u(638), 0.01, 2)

    # LOAD
    for ph, addr in zip(("l1", "l2", "l3"), (644, 645, 646)):
        m[f"deye.load.voltage_{ph}_v"] = scale(u(addr), 0.1, 1)
    # 647..649 were zero while the load was several kW, so they are NOT exposed
    # as load currents. Keep raw values for later identification.
    for ph, addr in zip(("l1", "l2", "l3"), (647, 648, 649)):
        m[f"deye.load.raw_reg{addr}"] = u(addr)
    for ph, addr in zip(("l1", "l2", "l3"), (650, 651, 652)):
        m[f"deye.load.power_{ph}_w"] = i(addr)
    m["deye.load.power_total_w"] = i(653)
    m["deye.load.frequency_hz"] = scale(u(655), 0.01, 2)

    # GENERATOR port - zero when inactive/not connected.
    for ph, addr in zip(("l1", "l2", "l3"), (661, 662, 663)):
        m[f"deye.gen.voltage_{ph}_v"] = scale(u(addr), 0.1, 1)
    m["deye.gen.power_total_w"] = i(667)

    # PV/MPPT area validated against live energy-flow data.
    # 672..675 track four active PV power channels and use 10 W/LSB.
    # 676..683 are the matching voltage/current pairs (0.1 V / 0.1 A).
    pv_power_total = 0
    pv_power_complete = True
    for n, (pa, va, ia) in enumerate(
        ((672, 676, 677), (673, 678, 679), (674, 680, 681), (675, 682, 683)),
        1
    ):
        p = None if u(pa) is None else u(pa) * 10
        v = scale(u(va), 0.1, 1)
        c = scale(u(ia), 0.1, 1)
        calc = None if v is None or c is None else round(v * c, 1)

        m[f"deye.pv.channel{n}.power_w"] = p
        m[f"deye.pv.channel{n}.voltage_v"] = v
        m[f"deye.pv.channel{n}.current_a"] = c
        m[f"deye.pv.channel{n}.calc_power_w"] = calc

        if p is None:
            pv_power_complete = False
        else:
            pv_power_total += p

    m["deye.pv.power_total_w"] = pv_power_total if pv_power_complete else None

    # The inverter model supports 6 MPPT, but the exact PV5/PV6 telemetry
    # addresses are not yet proven from this installation. Do not invent them.

    return m


def zabbix_send(metrics):
    lines = []

    for key, value in metrics.items():
        if value is None:
            continue

        if isinstance(value, str):
            value = json.dumps(
                value.replace("\n", " ").replace("\r", " "),
                ensure_ascii=False
            )

        lines.append(f"{ZABBIX_HOST} {key} {value}")

    data = "\n".join(lines) + "\n"

    cmd = [
        ZABBIX_SENDER,
        "-z", ZABBIX_SERVER,
        "-i", "-"
    ]

    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    stdout, stderr = p.communicate(data.encode("utf-8"))

    return (
        p.returncode,
        stdout.decode(errors="ignore"),
        stderr.decode(errors="ignore")
    )


def probe(mb, start=500, end=800, block=20):
    """
    Safe READ-ONLY scan.
    First tries block reads; falls back to individual reads.
    """
    print(f"# Elfin: {ELFIN_IP}:{ELFIN_PORT}")
    print(f"# Deye Modbus slave: {MODBUS_SLAVE_ID}")
    print(f"# FC03 READ-ONLY probe: {start}..{end}")
    print("#")
    print("# ADDR   HEX      U16      I16")

    addr = start

    while addr <= end:
        qty = min(block, end - addr + 1)

        try:
            regs = mb.read_holding_registers(addr, qty)

            for offset, raw in enumerate(regs):
                a = addr + offset
                print(
                    f"{a:5d}  0x{a:04X}  "
                    f"{raw:6d}  {s16(raw):7d}"
                )

        except Exception:
            for a in range(addr, addr + qty):
                try:
                    raw = read_u16(mb, a)
                    print(
                        f"{a:5d}  0x{a:04X}  "
                        f"{raw:6d}  {s16(raw):7d}"
                    )
                except Exception as e:
                    print(
                        f"{a:5d}  0x{a:04X}  "
                        f"------  READ_ERROR"
                    )

                time.sleep(0.03)

        addr += qty


def print_metrics(metrics):
    print()
    print(
        "---",
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "---"
    )

    for key in sorted(metrics):
        print(f"{key:42s} = {metrics[key]}")


def connection_test(mb):
    print(
        f"Connecting to Elfin EE11A "
        f"{ELFIN_IP}:{ELFIN_PORT} ..."
    )

    mb.connect()
    print("TCP connection to Elfin: OK")

    print(
        f"Testing Deye Modbus slave ID {MODBUS_SLAVE_ID}, "
        f"register 500 ..."
    )

    raw = read_u16(mb, 500)
    print(f"Modbus response: OK, register 500 = {raw}")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--test",
        action="store_true",
        help="test Elfin TCP + Deye Modbus communication"
    )

    parser.add_argument(
        "--probe",
        action="store_true",
        help="read-only scan registers 500..800"
    )

    parser.add_argument(
        "--probe-full",
        action="store_true",
        help="read-only scan registers 0..2000"
    )

    parser.add_argument(
        "--loop",
        action="store_true",
        help="keep polling every POLL_SEC seconds (default: one-shot)"
    )

    args = parser.parse_args()

    mb = ModbusTCP(
        ELFIN_IP,
        ELFIN_PORT,
        MODBUS_SLAVE_ID,
        SOCKET_TIMEOUT
    )

    try:
        if args.test:
            connection_test(mb)
            return 0

        if args.probe:
            mb.connect()
            probe(mb, 500, 800)
            return 0

        if args.probe_full:
            mb.connect()
            probe(mb, 0, 2000)
            return 0

        while True:
            try:
                if mb.sock is None:
                    mb.connect()

                metrics = collect(mb)
                print_metrics(metrics)

                rc, out, err = zabbix_send(metrics)

                if rc == 0:
                    print("Zabbix: sent OK")
                    if out.strip():
                        print(out.strip())
                else:
                    print(
                        "Zabbix error:",
                        err.strip() or out.strip()
                    )

            except FileNotFoundError:
                print("ERROR: zabbix_sender not found in PATH")

            except Exception as e:
                print("Polling error:", repr(e))
                mb.close()

            if not args.loop:
                return 0

            time.sleep(POLL_SEC)

    except KeyboardInterrupt:
        print("\nStopped by user")

    finally:
        mb.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
