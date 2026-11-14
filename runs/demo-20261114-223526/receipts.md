# Receipts

Budget 20 USDC | spent 9.078000 | held in escrow/disputes 0

| Part | Bought from | Quoted | Paid (incl. fee) | Check | Status |
|---|---|---|---|---|---|
| outline | self | - | 0 | passed | self_done |
| market_data | - | 9.500000 | 0 | FAILED | no_deal |
| competitor_scan | Scout Sue | 5.500000 | 5.610000 | passed | paid |
| risk_review | Riskbot | 3.400000 | 3.468000 | passed | paid |
| final_brief | self | - | 0 | passed | self_done |

## outline: Outline
- status: self_done; kind: make; seller: self
- price 0 + fee 0; paid out 0 USDC
- market job: None; bid: None
- check (deterministic): passed; all deterministic checks passed

## market_data: Market data
- status: no_deal; kind: buy; seller: -
- price 0 + fee 0; paid out 0 USDC
- market job: job-1; bid: None
- check (None): FAILED; 
- negotiation: counter 5.882352
- negotiation: walk_away  no revised quote

## competitor_scan: Competitor scan
- status: paid; kind: buy; seller: Scout Sue
- price 5.500000 + fee 0.110000; paid out 5.610000 USDC
- market job: job-2; bid: job-2-b0
- check (deterministic): passed; all deterministic checks passed
- negotiation: accept 5.500000 quote within planned spend
- payment: 5.610000 to mock-market.near op=op_mock_37516e04 status=SUCCESS
- note: 1 revision request(s) sent

## risk_review: Risk review
- status: paid; kind: buy; seller: Riskbot
- price 3.400000 + fee 0.068000; paid out 3.468000 USDC
- market job: job-3; bid: job-3-b0
- check (deterministic): passed; all deterministic checks passed
- negotiation: accept 3.400000 quote within planned spend
- payment: 3.468000 to mock-market.near op=op_mock_ce45e767 status=SUCCESS

## final_brief: Final brief
- status: self_done; kind: make; seller: self
- price 0 + fee 0; paid out 0 USDC
- market job: None; bid: None
- check (deterministic): passed; all deterministic checks passed
