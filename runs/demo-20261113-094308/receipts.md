# Receipts

Budget 20 USDC | spent 14.400000 | held in escrow/disputes 3.000000

| Part | Bought from | Quoted | Paid (incl. fee) | Check | Status |
|---|---|---|---|---|---|
| outline | self | - | 0 | passed | self_done |
| market_data | data-agent-a | 12.352941 | 9.000000 | passed | paid |
| competitor_scan | scan-agent | 5.294117 | 5.400000 | passed | paid |
| risk_review | risk-agent | 2.352941 | 0 | FAILED | withheld |
| final_brief | self | - | 0 | passed | self_done |

## outline: Outline
- status: self_done; kind: make; seller: self
- price 0 + fee 0; paid out 0 USDC
- market job: None; bid: None
- check (deterministic): passed; all deterministic checks passed

## market_data: Market data
- status: paid; kind: buy; seller: data-agent-a
- price 8.823529 + fee 0.176471; paid out 9.000000 USDC
- market job: job-1; bid: job-1-b0
- check (deterministic): passed; all deterministic checks passed
- negotiation: counter 8.823529
- negotiation: accept 8.823529 seller met the counter
- payment: 9.000000 to mock-market.near op=op_mock_12772cbb status=SUCCESS

## competitor_scan: Competitor scan
- status: paid; kind: buy; seller: scan-agent
- price 5.294117 + fee 0.105883; paid out 5.400000 USDC
- market job: job-2; bid: job-2-b0
- check (deterministic): passed; all deterministic checks passed
- negotiation: accept 5.294117 quote within planned spend
- payment: 5.400000 to mock-market.near op=op_mock_8a57bbb6 status=SUCCESS
- note: 1 revision request(s) sent

## risk_review: Risk review
- status: withheld; kind: buy; seller: risk-agent
- price 2.352941 + fee 0.047059; paid out 0 USDC
- market job: job-3; bid: job-3-b0
- check (deterministic): FAILED; too short: 5 words < 40; missing required term: regulatory; missing required term: liquidity
- negotiation: accept 2.352941 quote within planned spend
- payment: 2.400000 to mock-market.near op=op_mock_5bd53aec status=SUCCESS
- note: 1 revision request(s) sent
- note: check failed after revision; payment withheld and dispute opened; funds stay reserved

## final_brief: Final brief
- status: self_done; kind: make; seller: self
- price 0 + fee 0; paid out 0 USDC
- market job: None; bid: None
- check (deterministic): passed; all deterministic checks passed
