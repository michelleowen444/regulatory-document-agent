"""FileMaker WebDirect adapter. Site-specific selectors live here."""
import os
import re
import time
from pathlib import Path
from core import TYPES, Request, safe_name

SOURCE = 'https://uarb.novascotia.ca/fmi/webd/UARB15'

def collect(request: Request, folder: Path, headed=False) -> dict:
    from playwright.sync_api import sync_playwright, expect
    result = dict(matter=request.matter, category=request.category, source=SOURCE,
                  metadata={}, counts={}, documents=[], errors=[])
    files = folder / 'files'
    files.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        opts = dict(headless=not headed)
        if os.getenv('BROWSER_CHANNEL'):
            opts['channel'] = os.environ['BROWSER_CHANNEL']
        browser = pw.chromium.launch(**opts)
        context = browser.new_context(accept_downloads=True, viewport={'width': 1440, 'height': 1100})
        page = context.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(SOURCE, wait_until='domcontentloaded', timeout=90000)
            # FileMaker enables contenteditable after the text surface is clicked.
            field = page.locator('#b0p0o254i0i0r1 .text')
            field.click(timeout=90000)
            page.locator('#b0p0o254i0i0r1 .text[contenteditable="true"]').fill(request.matter)
            field.press('Tab')
            page.locator('#b0p0o258i0i0r1').click()
            page.locator('#b0p0o286i0i0r1').wait_for()
            expect(page.locator('#b0p0o286i0i0r1')).to_have_text(re.compile(r'M\d{5}'), timeout=30000)
            actual = page.locator('#b0p0o286i0i0r1').inner_text().strip()
            if actual != request.matter:
                raise RuntimeError(f'Matter mismatch: requested {request.matter}, received {actual}.')
            for category in TYPES:
                label = page.get_by_role('button', name=re.compile(r'^' + category + r'\s*-')).inner_text()
                match = re.fullmatch(re.escape(category) + r'\s*-\s*([\d,]+)', label.strip())
                if not match:
                    raise RuntimeError('Cannot read tab count: ' + label)
                result['counts'][category] = int(match[1].replace(',', ''))
            for label, obj in [('Category', 287), ('Status', 289), ('Date Received', 292),
                               ('Decision Date', 294), ('Outcome', 295), ('Type', 298)]:
                result['metadata'][label] = page.locator(f'#b0p0o{obj}i0i0r1').inner_text().strip()
            labels = page.locator('#b0p0o0i0i0r1 .v-label').all_inner_texts()
            result['metadata']['Title / Description'] = max(labels, key=len, default='Not extracted').strip()
            target = min(10, result['counts'][request.category])
            if not target:
                page.screenshot(path=str(folder / 'matter.png'), full_page=True)
                return result
            page.get_by_role('button', name=re.compile(r'^' + request.category + r'\s*-')).click()
            page.get_by_role('button', name='GO GET IT', exact=True).first.wait_for()
            page.screenshot(path=str(folder / 'matter.png'), full_page=True)
            seen = set()
            stagnant = 0
            deadline = time.monotonic() + 420
            while len(result['documents']) < target and stagnant < 4 and time.monotonic() < deadline:
                rows = page.get_by_role('row')
                candidates = []
                for row in rows.all():
                    fields = [s.strip() for s in row.locator('.fm-textarea .text').all_inner_texts()]
                    if not fields:
                        continue
                    # Same visible columns, different DOM order in each FileMaker layout.
                    title = row.locator('.fm_object_202 .text').inner_text().strip()
                    date = row.locator('.fm_object_203 .text').inner_text().strip()
                    security = row.locator('.fm_object_204 .text').inner_text().strip()
                    identities = [s for s in fields if s not in (title, date, security)
                                  and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9()._-]*', s)]
                    if not identities:
                        raise RuntimeError('Cannot identify document row: ' + repr(fields))
                    doc_id = identities[0]
                    if doc_id not in seen:
                        candidates.append([doc_id, title, date, security])
                if not candidates:
                    stagnant += 1
                    rows.last.hover()
                    page.mouse.wheel(0, 500)
                    page.wait_for_timeout(800)
                    continue
                stagnant = 0
                for fields in candidates:
                    if len(result['documents']) >= target:
                        break
                    doc_id, title, date, security = fields[:4]
                    seen.add(doc_id)
                    if security.lower() != 'public':
                        result['errors'].append(f'{doc_id}: skipped non-public entry.')
                        continue
                    # Screen slot IDs are recycled; reacquire by document identity.
                    row = page.get_by_role('row').filter(has=page.locator('.fm-textarea .text', has_text=re.compile('^' + re.escape(doc_id) + r'\s*$')))
                    try:
                        row.get_by_role('button', name='GO GET IT', exact=True).click()
                        close = page.get_by_role('button', name='Close', exact=True)
                        close.wait_for()
                        download_button = page.get_by_role('button', name=re.compile(r'^' + re.escape(doc_id) + r'\.', re.I))
                        if download_button.count() == 0:
                            download_button = page.locator('.v-window').get_by_role('button').filter(has_text=re.compile(r'\.[A-Za-z0-9]{2,5}$')).first
                        with page.expect_download(timeout=60000) as event:
                            download_button.click()
                        download = event.value
                        name = doc_id + '-' + safe_name(download.suggested_filename)
                        path = files / name
                        download.save_as(str(path))
                        if not path.stat().st_size:
                            raise RuntimeError('Empty download')
                        if path.suffix.lower() == '.pdf' and not path.read_bytes().startswith(b'%PDF-'):
                            path.unlink()
                            raise RuntimeError('Expected PDF but received another format')
                        result['documents'].append(dict(id=doc_id, title=title, date=date, filename=name))
                        print(f"Downloaded {len(result['documents'])}/{target}: {doc_id}", flush=True)
                    except Exception as exc:
                        result['errors'].append(f'{doc_id}: {type(exc).__name__}: {str(exc).splitlines()[0][:180]}')
                    finally:
                        close = page.get_by_role('button', name='Close', exact=True)
                        if close.count() and close.is_visible():
                            close.click()
            if len(result['documents']) < target:
                if len(seen) >= result['counts'][request.category]:
                    result['errors'].append('All listed entries were examined; some were non-public or failed to download.')
                else:
                    result['errors'].append(f"Only {len(result['documents'])} of the target {target} files downloaded; list traversal was incomplete.")
            return result
        except Exception:
            page.screenshot(path=str(folder / 'failure.png'), full_page=True)
            raise
        finally:
            context.close()
            browser.close()




