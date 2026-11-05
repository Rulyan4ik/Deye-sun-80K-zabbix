# Deye SUN-80K → Elfin EE11A → Zabbix

[Українська](README.md) | **English**

Monitor a **Deye SUN-80K-SG02HP3-EU-EM6** inverter in Zabbix using an **Elfin EE11A** Ethernet gateway.

The Python script reads inverter registers over Modbus TCP and sends metrics through `zabbix_sender`. It uses **FC03 (Read Holding Registers) only** and does not change inverter settings.

```text
Python script ── Modbus TCP ── Elfin EE11A ── RS485 / Modbus RTU ── Deye SUN-80K
      │
      └── zabbix_sender ── Zabbix server / proxy
```

## Files

| File | Purpose |
| --- | --- |
| [deye_sun80k_elfin_zabbix_v4_1.py](deye_sun80k_elfin_zabbix_v4_1.py) | Polling, Warning/Fault decoding, and metric delivery |
| [template_deye_sun80k_zabbix_7_4_fix.yaml](template_deye_sun80k_zabbix_7_4_fix.yaml) | **Zabbix 7.4** template with 71 Zabbix trapper items |

Metrics cover the grid, load, battery, inverter, generator port, temperatures, daily PV energy, and four verified PV channels. Warning and fault summaries include original names and Ukrainian translations. Template labels and descriptions are Ukrainian. PV5/PV6 telemetry addresses are not defined in this script.

## Requirements

- Linux with Python 3 and `zabbix_sender`.
- Zabbix 7.4 to import the supplied template; other versions have not been tested.
- Network access from the script host to Elfin and the Zabbix server/proxy.
- Elfin EE11A with **Modbus TCP ↔ Modbus RTU** conversion enabled. RS485 settings must match the inverter.

No third-party Python packages are needed to run the script. Register meanings and scaling depend on the inverter model and firmware; compare readings with the inverter display when using different equipment.

## Download and install

Choose **Code → Download ZIP** on GitHub, or clone the repository:

```bash
git clone https://github.com/Rulyan4ik/Deye-sun-80K-zabbix.git
cd Deye-sun-80K-zabbix
```

On Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install python3 zabbix-sender
command -v zabbix_sender
```

If the package is unavailable in your distribution, install it from the official Zabbix repository for your OS.

## Configure

Edit the `CONFIG` section near the top of the Python file:

```python
ELFIN_IP = "192.168.31.100"  # your Elfin address
ELFIN_PORT = 8899           # TCP port configured on your Elfin
MODBUS_SLAVE_ID = 0         # your inverter's Modbus address

SOCKET_TIMEOUT = 3.0
POLL_SEC = 30

ZABBIX_SERVER = "127.0.0.1"  # your Zabbix server/proxy address
ZABBIX_HOST = "Deye_80k"     # exact Host name field in Zabbix
ZABBIX_SENDER = "/usr/bin/zabbix_sender"
```

These are defaults from the supplied working script, not automatically detected network settings. Configure the script in its source; it does not read these settings from environment variables. Update `ZABBIX_SENDER` if `command -v zabbix_sender` reports a different location.

### Zabbix

1. Open **Data collection → Templates → Import** and import the YAML file.
2. Create or select a host. Its **Host name** must exactly match `ZABBIX_HOST` (default: `Deye_80k`).
3. Link the **Deye SUN-80K SG02HP3** template to that host.
4. Allow connections from the script host to the Zabbix server/proxy on TCP port **10051**. If trapper items have an Allowed hosts restriction, allow the sender's address.

The template uses trapper items; a Zabbix agent is not required for this script. The script uses the default sender port and does not expose settings for a different port or TLS.

## Check and run

Test TCP connectivity and read register 500 without sending data to Zabbix:

```bash
python3 deye_sun80k_elfin_zabbix_v4_1.py --test
```

Poll once and send metrics:

```bash
python3 deye_sun80k_elfin_zabbix_v4_1.py
```

Poll continuously at the `POLL_SEC` interval:

```bash
python3 deye_sun80k_elfin_zabbix_v4_1.py --loop
```

Press `Ctrl+C` to stop. Check values under **Monitoring → Latest data** and inspect the sender's `processed`, `failed`, and `total` counts. A zero script exit code alone is insufficient: the script can print a polling error and still exit with code 0.

Additional read-only diagnostic modes do not send metrics:

```bash
python3 deye_sun80k_elfin_zabbix_v4_1.py --probe       # registers 500..800
python3 deye_sun80k_elfin_zabbix_v4_1.py --probe-full  # registers 0..2000
```

A full scan can take a long time when addresses are rejected or the device does not respond.

## Known behavior

- The script produces **80 metrics**, while the template contains **71 keys**. Nine raw diagnostic metrics have no matching template items: `deye.warning1.raw`, `deye.warning2.raw`, `deye.fault1.raw`–`deye.fault4.raw`, and `deye.load.raw_reg647`–`deye.load.raw_reg649`. Zabbix may reject these and report partial batch acceptance. Add matching numeric trapper items to your own template if you want to receive them.
- `deye.comm.ok` is set to 1 before register reads. It alone does not guarantee successful data collection; check telemetry values and error messages.
- Missing values (`None`) are not sent. Alarm-register read failures appear as the Ukrainian text “Помилка читання Modbus” (“Modbus read error”).

