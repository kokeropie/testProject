import signal
import sys
from datetime import datetime
from pathlib import Path

from confluent_kafka import Consumer, KafkaError, KafkaException

from utils import ConfigError, check_dir_writable, load_config, setup_logging


def validate_config(cfg: dict) -> None:
    kafka = cfg.get("kafka", {})
    for field in ("bootstrap_servers", "topic", "group_id"):
        if not kafka.get(field):
            raise ConfigError(f"config.json: kafka.{field} is required")
    check_dir_writable(
        Path(cfg["paths"]["json_ingestion_dir"]), "JSON ingestion directory"
    )


def build_kafka_config(cfg: dict) -> dict:
    kafka = cfg["kafka"]
    conf = {
        "bootstrap.servers": kafka["bootstrap_servers"],
        "group.id": kafka["group_id"],
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    }
    protocol = kafka.get("security_protocol", "PLAINTEXT").upper()
    if protocol != "PLAINTEXT":
        conf["security.protocol"] = protocol
        conf["sasl.mechanism"] = kafka.get("sasl_mechanism", "")
        conf["sasl.username"] = kafka.get("sasl_username", "")
        conf["sasl.password"] = kafka.get("sasl_password", "")
    return conf


def write_message(payload: bytes, offset: int, ingestion_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"msg_{timestamp}_{offset}.json"
    dest = ingestion_dir / filename
    dest.write_bytes(payload)
    return dest


def run() -> None:
    cfg = load_config()
    validate_config(cfg)
    log = setup_logging(cfg, "CONSUMER")

    ingestion_dir = Path(cfg["paths"]["json_ingestion_dir"])
    kafka_cfg = build_kafka_config(cfg)
    topic = cfg["kafka"]["topic"]

    consumer = Consumer(kafka_cfg)
    shutdown = False

    def _handle_signal(sig, frame):
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        consumer.subscribe([topic])
        log.info(
            f"Connected to {cfg['kafka']['bootstrap_servers']}, "
            f"topic={topic}, group={cfg['kafka']['group_id']}"
        )

        while not shutdown:
            msg = consumer.poll(timeout=1.0)

            if msg is None:
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(msg.error())

            dest = write_message(msg.value(), msg.offset(), ingestion_dir)
            consumer.commit(message=msg, asynchronous=False)
            log.info(f"Written {dest.name} (offset={msg.offset()})")

    finally:
        consumer.close()
        log.info("Shutdown complete")


if __name__ == "__main__":
    try:
        run()
    except ConfigError as e:
        print(f"[CONFIG ERROR] {e}", file=sys.stderr)
        sys.exit(1)
