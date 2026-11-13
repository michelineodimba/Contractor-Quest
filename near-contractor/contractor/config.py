import os
from dataclasses import dataclass
from decimal import Decimal


class ConfigError(RuntimeError):
    pass


def require(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise ConfigError("missing environment variables: " + ", ".join(missing))
    return {n: os.environ[n] for n in names}


@dataclass
class LiveSettings:
    market_url: str
    market_key: str
    deposit_address: str
    agent_api_url: str
    agent_api_key: str
    grant_token: str
    agent_id: str
    asset_id: str
    decimals: int
    currency: str
    fee_rate: Decimal

    @classmethod
    def from_env(cls) -> "LiveSettings":
        e = require("NEAR_MARKET_API_KEY", "MARKET_DEPOSIT_ADDRESS", "NEAR_INTENTS_AGENT_API_KEY",
                    "AGENT_ID", "AGENT_GRANT_TOKEN")
        g = os.environ.get
        return cls(g("NEAR_MARKET_URL", "https://market.near.ai"), e["NEAR_MARKET_API_KEY"],
                   e["MARKET_DEPOSIT_ADDRESS"],
                   g("AGENT_API_BASE_URL", "https://api.demo.agentsonintents.com"),
                   e["NEAR_INTENTS_AGENT_API_KEY"], e["AGENT_GRANT_TOKEN"], e["AGENT_ID"],
                   # VERIFY with `python -m contractor verify` (GET /v1/tokens): native USDC on NEAR
                   g("ASSET_ID", "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1"),
                   int(g("ASSET_DECIMALS", "6")), g("CURRENCY", "USDC"), Decimal(g("FEE_RATE", "0.02")))
