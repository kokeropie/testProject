# Kafka-to-Excel Data Pipeline (KEDP)

Consumes JSON messages from a Kafka topic, saves them as individual files, and compiles them into a daily Excel report at 01:00 AM Jakarta time.

```
[ Kafka Broker ]
      │
      ▼  consumer.py  (always-on)
[ Raw_JSON\  *.json files ]
      │
      ▼  compiler.py  (daily 01:00 AM)
[ Daily_Reports\  Compiled_Report_YYYY-MM-DD.xlsx ]
```

---

## Requirements

- Windows 10 Home / Pro
- Python 3.9+
- System timezone set to **(UTC+07:00) Bangkok, Hanoi, Jakarta**

---

## Setup

**1. Install dependencies**

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

**2. Create your data folders**

```bat
mkdir C:\KafkaData\Raw_JSON
mkdir C:\KafkaData\Daily_Reports
```

**3. Edit `config.json`**

```json
{
  "kafka": {
    "bootstrap_servers": "192.168.1.50:9092",
    "topic": "update-client",
    "group_id": "kedp-consumer-01",
    "security_protocol": "PLAINTEXT",
    "sasl_mechanism": "",
    "sasl_username": "",
    "sasl_password": ""
  },
  "paths": {
    "json_ingestion_dir": "C:\\KafkaData\\Raw_JSON",
    "excel_output_dir":   "C:\\KafkaData\\Daily_Reports"
  },
  "log": {
    "max_bytes": 5242880,
    "backup_count": 3
  }
}
```

Set `security_protocol` to `SASL_PLAINTEXT` and fill the `sasl_*` fields if your broker requires authentication. Leave them blank for unauthenticated connections.

**4. Register the scheduled tasks** (run each once as Administrator)

```bat
register_consumer_task.bat    # starts consumer.py on every login
register_compiler_task.bat    # runs compiler.py daily at 01:00 AM
```

Both scripts validate that Python and the script are found before registering. Re-running is safe — `/F` overwrites the existing task.

---

## File Structure

```
KEDP\
├── consumer.py                  # Kafka consumer — writes .json files
├── compiler.py                  # Excel compiler — one-shot, run by Task Scheduler
├── utils.py                     # Shared config loading and logging
├── config.json                  # All user settings — edit this, not the scripts
├── pipeline.log                 # Rolling log (max 5 MB × 3 backups)
├── requirements.txt
├── register_consumer_task.bat   # Registers consumer Task Scheduler job
└── register_compiler_task.bat   # Registers compiler Task Scheduler job

C:\KafkaData\
├── Raw_JSON\                    # Incoming .json files land here
│   └── Archive\                 # Processed files moved here after each compile
│       └── Errors\              # Corrupt files that failed to parse
└── Daily_Reports\               # Output .xlsx files
```

---

## How It Works

### consumer.py

Polls the Kafka topic continuously. For each message:

1. Writes the payload to `Raw_JSON\msg_YYYYMMDD_HHmmss_<offset>.json`
2. Commits the Kafka offset only after the file is flushed — no data loss on crash

Starts automatically on login via Task Scheduler (30-second delay for network readiness). Shuts down cleanly on `Ctrl+C` or `SIGTERM`.

### compiler.py

Runs once at 01:00 AM. For each `.json` file in `Raw_JSON\`:

1. Parses the JSON — corrupt files are skipped, logged, and moved to `Errors\`
2. Flattens all messages into a single DataFrame — missing keys become `NaN`
3. Sorts columns alphabetically for consistent layout across days
4. Writes `Daily_Reports\Compiled_Report_YYYY-MM-DD.xlsx`
5. Moves all successfully parsed files to `Archive\`

If the folder is empty at 01:00 AM, the script exits without creating an empty workbook.

---

## Logs

Both scripts append to `pipeline.log` in the project folder.

```
2026-06-23 00:58:11 INFO  [CONSUMER] Connected to 192.168.1.50:9092, topic=update-client, group=kedp-consumer-01
2026-06-23 00:58:12 INFO  [CONSUMER] Written msg_20260623_005812_10040.json (offset=10040)
2026-06-23 01:00:01 INFO  [COMPILER] Started. Found 847 JSON file(s).
2026-06-23 01:00:03 ERROR [COMPILER] Skipped msg_20260622_144301_9981.json — invalid JSON
2026-06-23 01:00:09 INFO  [COMPILER] Done. Files processed: 846, errors: 1, rows: 846, output: Compiled_Report_2026-06-23.xlsx, elapsed: 8.2s
```

The log rotates at 5 MB and keeps 3 backups. Both limits are configurable in `config.json`.

---

## Useful Task Scheduler Commands

```bat
:: Consumer
schtasks /Query  /TN "KEDP_KafkaConsumer" /FO LIST /V
schtasks /Run    /TN "KEDP_KafkaConsumer"
schtasks /End    /TN "KEDP_KafkaConsumer"
schtasks /Delete /TN "KEDP_KafkaConsumer" /F

:: Compiler
schtasks /Query  /TN "KEDP_DailyCompiler" /FO LIST /V
schtasks /Run    /TN "KEDP_DailyCompiler"
schtasks /Delete /TN "KEDP_DailyCompiler" /F
```

---

## Running Manually

```bat
:: Activate venv first
venv\Scripts\activate

:: Start consumer (Ctrl+C to stop)
python consumer.py

:: Run compiler immediately (processes whatever is in Raw_JSON\ right now)
python compiler.py
```
