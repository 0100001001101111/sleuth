"""Payment providers. This package is the ONLY place that knows about money. It is never imported by sleuth.* or by
service.jobs / service.monitor, and it holds no signing keys: the pilot providers are 'manual' (operator confirms a
stablecoin transfer to a receive-only address) and 'x402' (flag-gated, mock facilitator in tests). Payment chain is
Base USDC; it is independent of the investigation chain."""
from .base import STATES, TRANSITIONS, PaymentError, advance, create, get  # noqa: F401
