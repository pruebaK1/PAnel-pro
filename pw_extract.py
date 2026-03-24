#!/usr/bin/env python3
import sys
import asyncio
import json

async def extract(url):
    try:
        from playwright.async_api import async_playwright
        m3u8_url     = None
        m3u8_headers = {}

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox','--disable-setuid-sandbox',
                      '--disable-dev-shm-usage','--disable-gpu']
            )
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36'
            )

            async def handle_request(request):
                nonlocal m3u8_url, m3u8_headers
                if '.m3u8' in request.url and not m3u8_url:
                    m3u8_url     = request.url
                    m3u8_headers = dict(request.headers)

            page = await context.new_page()
            page.on('request', handle_request)

            await page.goto(url, wait_until='domcontentloaded', timeout=20000)
            await asyncio.sleep(3)

            for selector in [
                'button[class*="reload"]','button[class*="restart"]',
                'button[class*="play"]','.reload-btn','.restart-button',
                '[class*="reload"]','[class*="restart"]','button',
            ]:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        await el.click()
                        await asyncio.sleep(3)
                        if m3u8_url: break
                except: pass

            for _ in range(15):
                if m3u8_url: break
                await asyncio.sleep(1)

            cookies = await context.cookies()
            cookie_str = '; '.join([f"{c['name']}={c['value']}" for c in cookies])
            if cookie_str:
                m3u8_headers['cookie'] = cookie_str

            await browser.close()

            if m3u8_url:
                # Incluir TODOS los headers relevantes
                keep = ['referer','origin','user-agent','cookie',
                        'sec-ch-ua','sec-ch-ua-mobile','sec-ch-ua-platform']
                filtered = {k: v for k, v in m3u8_headers.items()
                           if k.lower() in keep}
                return {'url': m3u8_url, 'headers': filtered}
            return None

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return None

if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(1)
    result = asyncio.run(extract(sys.argv[1]))
    if result:
        if len(sys.argv) > 2 and sys.argv[2] == '--json':
            print(json.dumps(result))
        else:
            print(result['url'])
    else:
        sys.exit(1)
