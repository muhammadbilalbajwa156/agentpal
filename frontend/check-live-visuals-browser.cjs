// Compatibility entry point for the restored original live layouts.
// The design browser runner preserves nonce/CSP, no-external-request, no-extra-read,
// retained-grid, CSV, filtering, sorting, responsive and console assertions.
// Set AGENTPAL_TEST_ORIGIN, AGENTPAL_TEST_TOKEN_FILE and AGENTPAL_TEST_SHOTS as before.
require('./check-live-design-browser.cjs');
