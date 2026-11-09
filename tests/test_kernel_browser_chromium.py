"""Real local Chromium exercises the fixed remote script, without Kernel credits."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from agentpal.tools.kernel_browser import CAPTURE_JS


def test_fixed_program_in_real_chromium_blocks_writes_cookies_and_private_redirects():
    node, spec = shutil.which("node"), importlib.util.find_spec("playwright")
    chrome = Path("/opt/pw-browsers/chromium-1243/chrome-linux64/chrome")
    if not node or not spec or not chrome.exists():
        pytest.skip("Local Chromium/Node/Playwright unavailable; SDK and Node guard tests still run")
    package = Path(spec.origin).parent / "driver" / "package"
    harness = r"""
const fs = require('node:fs');
const http = require('node:http');
const options = JSON.parse(fs.readFileSync(0, 'utf8'));
const {chromium} = require(options.package);
require('node:dns/promises').lookup = async () => [{address: '93.184.216.34'}];
const seen = [];
const server = http.createServer((req, res) => {
  seen.push({path:req.url, method:req.method, cookie:req.headers.cookie || ''});
  if (req.url === '/start') {res.writeHead(302, {location:'https://final.example/final'}); res.end();}
  else if (req.url === '/private') {res.writeHead(302, {location:'http://127.0.0.1/forbidden'}); res.end();}
  else if (req.url === '/final') {
    res.writeHead(200, {'content-type':'text/html', 'set-cookie':'serversecret=value'});
    res.end(`<html><head><title>Merchant</title></head><body>Price 12.00
      <script>document.cookie='clientsecret=value';fetch('/ping');fetch('/write',{method:'POST'});</script>
      <img src="http://169.254.169.254/latest/meta-data/">
      </body></html>`);
  } else {res.writeHead(200, {'content-type':'text/plain'}); res.end('ok');}
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless:true, executablePath:options.chrome, args:['--no-sandbox']});
  const originalNewContext = browser.newContext.bind(browser);
  // Test transport adapter: public merchant names map only to this fixture server.
  // The production route guard still sees the original public URL at every hop.
  browser.newContext = async opts => {
    const context = await originalNewContext(opts);
    const originalRoute = context.route.bind(context);
    context.route = async (pattern, handler) => originalRoute(pattern, async route => {
      const originalFetch = route.fetch.bind(route);
      route.fetch = opts => originalFetch({...opts,
        url:`http://127.0.0.1:${server.address().port}${new URL(route.request().url()).pathname}`});
      return handler(route);
    });
    return context;
  };
  try {
    const execute = new (async function(){}).constructor('browser','input',options.code);
    const result = await execute(browser, {url:'https://shop.example/start',timeout_ms:10000});
    const blocked = await execute(browser, {url:'https://shop.example/private',timeout_ms:10000});
    process.stdout.write(JSON.stringify({result, blocked, seen}));
  } finally {await browser.close(); await new Promise(resolve=>server.close(resolve));}
})().catch(error=> {process.stderr.write(error.name); process.exitCode=1; server.close();});
"""
    completed = subprocess.run([node, "-e", harness], text=True, capture_output=True,
                               input=json.dumps(dict(code=CAPTURE_JS, package=str(package),
                                                     chrome=str(chrome))), timeout=25, check=True)
    result = json.loads(completed.stdout)
    assert result["result"]["final_url"] == "https://final.example/final"
    assert result["result"]["redirect_chain"] == ["https://shop.example/start", "https://final.example/final"]
    assert result["result"]["http_status"] == 200
    assert result["result"]["title"] == "Merchant"
    assert "Price 12.00" in result["result"]["visible_text"]
    assert result["blocked"] == {"failure": "blocked_target"}
    assert all(item["method"] == "GET" and item["cookie"] == "" for item in result["seen"])
    assert {item["path"] for item in result["seen"]} <= {"/start", "/final", "/ping", "/private"}
