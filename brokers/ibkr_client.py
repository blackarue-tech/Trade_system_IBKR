from ib_insync import IB, util
from dataclasses import dataclass
import json, os, time

CONFIG_IBKR = os.path.join("configs", "ibkr.json")
_clients = {}

@dataclass
class IbkrConnParams:
    host: str
    port: int
    clientId: int
    timeout: int


def get_client(role: str) -> "IbkrClient":
    c = _clients.get(role)
    if c is None:
        c = IbkrClient(role)
        _clients[role] = c
    return c

def _load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

class IbkrClient:
    def __init__(self, role: str):
        cfg = _load_json(CONFIG_IBKR)
        self.role = role
        self.params = IbkrConnParams(
            host = cfg.get("host","127.0.0.1"),
            port = int(cfg.get("port", 4002)),   # default paper
            clientId = int(cfg["client_ids"][role]),
            timeout = int(cfg.get("connect_timeout_secs", 8)),
        )
        self.backoff = int(cfg.get("reconnect_backoff_secs", 5))
        self.ib = IB()

    def connect(self) -> bool:
        if self.ib.isConnected():
            return True

        # try configured port first, then common alternates (TWS/Gateway)
        port_candidates = [self.params.port, 7497, 7496, 4002, 4001]
        seen_ports = set()
        last_err = None

        for port in port_candidates:
            if port in seen_ports: 
                continue
            seen_ports.add(port)

            # try a small window of clientIds to dodge collisions
            base = int(self.params.clientId)
            for cid in [base + i for i in range(10)]:
                try:
                    if self.ib.isConnected():
                        self.ib.disconnect()
                    self.ib.connect(self.params.host, port, clientId=cid, timeout=self.params.timeout)
                    if self.ib.isConnected():
                        self.params.clientId = cid
                        self.params.port = port
                        print(f"✅ Connected {self.params.host}:{port} as clientId={cid} (role={self.role})")
                        return True
                except Exception as e:
                    last_err = e
                    msg = str(e)
                    # duplicate or peer-close → try next cid on same port
                    if ("clientId" in msg) or ("Peer closed connection" in msg) or ("already in use" in msg):
                        continue
                    # hard network failure → break to next port
                    break

        if last_err:
            raise last_err
        return False

    def ensure(self) -> IB:
        if not self.ib.isConnected():
            ok = self.connect()
            if not ok:
                time.sleep(self.backoff)
                ok = self.connect()
                if not ok:
                    raise RuntimeError("IBKR connect failed after retries.")
        return self.ib

    def disconnect(self):
        try: self.ib.disconnect()
        except Exception: pass

    # Health check
    def ping(self) -> bool:
        try:
            ib = self.ensure()
            # request current time as liveness
            _ = ib.reqCurrentTime()
            return True
        except Exception:
            return False
