# Test Fixtures - Controlled Fake Site for Browser Tests

**These fixtures are the ONLY site these tests may touch. No test may make a real network request.**

All HTML files in this directory are static and contain NO JavaScript that exfiltrates or executes any code. Forms use plain GET/POST methods to simulate typical web interactions.

## Files

- `pizza.html` - A pizza search page with a form (method="get" action="search.html") and a navigation link
- `checkout.html` - A food order page with items, quantities, hidden price field, and "Place order" button  
- `train.html` - A journey search page with origin/destination/date fields, class dropdown, price display, and "Book & Pay" button
- `evil.html` - A prompt-injection stress test page with visible benign content plus text blocks styled like system instructions

## Important Rules

1. **NO REAL NETWORK**: All browser tests MUST route through `FakeDriver` scriptable pages, not actual HTTP requests
2. **STATIC ONLY**: These HTML files have zero JavaScript execution - they are purely structural DOM tests
3. **FAKE DOMAIN**: URLs reference `fixtures.invalid` domain - this is NOT a real website, it's simulated entirely by FakeDriver

## Fixture Purpose

These controlled pages allow tests to verify:
- URL scheme validation (only http/https allowed)
- Ref staleness after navigation
- Text redaction of credentials/password-like patterns
- Prompt injection defense (page text never triggers actions)
- Form submission simulation (clicking buttons navigates to next page in FakeDriver)
