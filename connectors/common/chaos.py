import os
import random
import time
from typing import Any


class ChaosEngine:
    """Deterministic fault injection for testing reliability.

    Controlled by CHAOS_SEED env var. When set, injects deterministic
    faults: timeouts, 503 errors, slow responses, and 'succeeded but
    response lost' scenarios.
    """

    def __init__(self) -> None:
        seed = os.environ.get("CHAOS_SEED")
        self.enabled: bool = seed is not None
        self.rng: random.Random | None = random.Random(int(seed)) if seed else None
        self._call_count: int = 0

    def maybe_fail(self, tool_name: str, connector_name: str) -> None:
        """Possibly inject a fault before tool execution.

        Raises RuntimeError for 503-style errors.
        Sleeps for slow response simulation.
        """
        if not self.enabled or not self.rng:
            return

        self._call_count += 1
        roll = self.rng.random()

        # 15% chance of fault on write operations
        if tool_name.startswith(("create_", "update_", "set_", "add_", "post_", "request_", "decide_")):
            if roll < 0.08:
                raise RuntimeError(
                    f"503 Service Unavailable: {connector_name}.{tool_name} temporarily unavailable"
                )
            elif roll < 0.12:
                # Slow response
                delay = self.rng.uniform(2.0, 5.0)
                time.sleep(delay)
            elif roll < 0.15:
                # Timeout after potentially succeeding - the dangerous case
                raise TimeoutError(
                    f"Request to {connector_name}.{tool_name} timed out after send - action may have succeeded"
                )

        # 5% chance on read operations
        elif roll < 0.05:
            raise RuntimeError(
                f"503 Service Unavailable: {connector_name}.{tool_name} temporarily unavailable"
            )


# Singleton
chaos = ChaosEngine()
