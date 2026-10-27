# 08 — Decisions only Bo can make

| # | Decision | Options | What changes |
|---|---|---|---|
| B-1 | First payer | (a) victims via the $29 case, (b) machine customers via the x402 screen, (c) launchpad partnership (Pons) | The build serves all three doors; marketing, copy and which flag is turned on first |
| B-2 | Validate before charging? | (a) Run the one-week test first (free screens to 20 recent real victims; target ~5 willing to pre-pay; watch for any exchange/Chainabuse action), or (b) open the $29 tier now | The docs recommend (a) as a gate, but the code does not enforce it: checkout turns on as soon as `SLEUTH_PAY_TO_BASE_USDC` is set. Until Bo decides, the variable stays unset and the gate is a recommendation, not a rule. Also decides whether $29 or $49 copy is tested |
| B-3 | Jurisdiction and counsel | Engage counsel on CA BSIS / NV NRS 648 private-investigator definitions before any assisted-reporting or human-reviewed tier | Whether the $249 tier ever exists and how it is worded |
| B-4 | Payment providers | Coinbase Payment Acceptance (or other) for checkout; CDP (or other) facilitator for x402 on Base | Requires Bo's accounts, KYB and a receiving address; nothing is configured now |
| B-5 | Hosting | Mac mini staging only vs small Linux VM (~$50–200/month all-in) for production | When "monitoring active" can honestly be claimed; paid RPC purchase |
| B-6 | Public wording | Is "wallet-drain beta" acceptable on the public site; is "no human investigation included" acceptable on the $29 tier | Site copy and refund policy |
| B-7 | Refund / credit policy | Automatic credit for partial coverage caused by provider limits; refund if the result "merely restates explorer data"? | Payment adapter configuration, unit economics |
| B-8 | Retention | Default private-case retention (proposed 12 months after close) | Schema defaults, privacy page |
| B-9 | Public pages | Ship search / methodology / corrections pages at all, or keep everything behind capability links until validation | Website scope |
| B-10 | Budget for external spend | Any spend at all in the next phase (RPC, email, facilitator test transactions) | Which backlog items can leave staging |
