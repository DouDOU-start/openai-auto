// ==UserScript==
// @name         PayPal Auto Filler
// @namespace    http://tampermonkey.net/
// @version      30.0
// @description  Auto-fill PayPal/OpenAI checkout pages
// @match        https://www.paypal.com/*
// @match        https://pay.openai.com/*
// @match        https://checkout.stripe.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @connect      meiguodizhi.com
// @connect      mail-api.yuecheng.shop
// @run-at       document-idle
// ==/UserScript==

// ========== 配置 ==========
var CONFIG = {
    phone: '5822599791', // 电话号码
    cardExpiry: '03 / 30', // 有效期
    cardCvv: '996', // CVV
    cardNumber: '1234561234568888', // 卡号兜底
    smsPollSeconds: 2,
    smsTimeoutSeconds: 180,
    smsNumbers: []
};
// ========================

let cachedProfile = null;
let selectedSmsNumber = null;
let securityCodeHandling = false;
let smsPollingActive = false;
let lastSecurityFillAt = 0;
let securityFillAttempts = 0;
let securityAutoStopped = false;
let debugPanelNode = null;
let debugLogLines = [];
let securityWatcherTicks = 0;
var US_FALLBACK_ADDRESS = { street:'1600 Amphitheatre Pkwy', city:'Mountain View', state:'California', stateCode:'CA', zip:'94043' };
var US_STATE_ALIASES = {
    AL:'Alabama', AK:'Alaska', AZ:'Arizona', AR:'Arkansas', CA:'California', CO:'Colorado', CT:'Connecticut', DE:'Delaware', DC:'District of Columbia', FL:'Florida', GA:'Georgia', HI:'Hawaii', ID:'Idaho', IL:'Illinois', IN:'Indiana', IA:'Iowa', KS:'Kansas', KY:'Kentucky', LA:'Louisiana', ME:'Maine', MD:'Maryland', MA:'Massachusetts', MI:'Michigan', MN:'Minnesota', MS:'Mississippi', MO:'Missouri', MT:'Montana', NE:'Nebraska', NV:'Nevada', NH:'New Hampshire', NJ:'New Jersey', NM:'New Mexico', NY:'New York', NC:'North Carolina', ND:'North Dakota', OH:'Ohio', OK:'Oklahoma', OR:'Oregon', PA:'Pennsylvania', RI:'Rhode Island', SC:'South Carolina', SD:'South Dakota', TN:'Tennessee', TX:'Texas', UT:'Utah', VT:'Vermont', VA:'Virginia', WA:'Washington', WV:'West Virginia', WI:'Wisconsin', WY:'Wyoming', AS:'American Samoa', GU:'Guam', MP:'Northern Mariana Islands', PR:'Puerto Rico', VI:'Virgin Islands'
};

(function() {
    'use strict';
    var log = function(s) {
        var text = String(s || '');
        console.log('[PP] ' + text);
        debugLogLines.push(new Date().toLocaleTimeString() + ' ' + text);
        if (debugLogLines.length > 12) debugLogLines = debugLogLines.slice(-12);
        renderDebugPanel();
    };
    applyRuntimeConfig();
    ensureDebugPanel();
    log('自动填充脚本已注入，版本 30.0-debug');

    // 隐藏验证码和地址补全
    var st = document.createElement('style');
    st.textContent = '#captcha-standalone,.captcha-overlay,.captcha-container,.AddressAutocomplete-results{display:none!important;height:0!important;overflow:hidden!important}';
    document.head.appendChild(st);

    function ensureDebugPanel() {
        if (debugPanelNode || !currentHost().includes('paypal.com')) {
            return;
        }
        debugPanelNode = document.createElement('div');
        debugPanelNode.id = 'protocol-reg-checkout-debug';
        debugPanelNode.style.cssText = [
            'position:fixed',
            'left:12px',
            'bottom:12px',
            'z-index:2147483647',
            'width:min(520px,calc(100vw - 24px))',
            'max-height:38vh',
            'overflow:auto',
            'background:rgba(14,17,20,.92)',
            'color:#d7fff6',
            'border:1px solid rgba(87,255,222,.35)',
            'border-radius:12px',
            'box-shadow:0 12px 40px rgba(0,0,0,.35)',
            'padding:10px 12px',
            'font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace',
            'white-space:pre-wrap',
            'pointer-events:none'
        ].join(';');
        (document.body || document.documentElement).appendChild(debugPanelNode);
        renderDebugPanel();
    }

    function renderDebugPanel() {
        if (!debugPanelNode) {
            ensureDebugPanel();
        }
        if (!debugPanelNode) {
            return;
        }
        var sms = currentSmsNumber();
        var prompt = false;
        try {
            prompt = hasSecurityCodePromptContent();
        } catch (e) {}
        debugPanelNode.textContent = [
            'Protocol Reg Checkout Debug',
            'host=' + currentHost() + ' path=' + currentPath(),
            'smsPhone=' + (sms.phone || '-') + ' smsUrl=' + (sms.smsUrl ? maskUrl(sms.smsUrl) : '-'),
            'securityPrompt=' + prompt + ' handling=' + securityCodeHandling + ' watcherTicks=' + securityWatcherTicks,
            'smsPolling=' + smsPollingActive,
            'fillAttempts=' + securityFillAttempts,
            'autoStopped=' + securityAutoStopped,
            'cachedCode=' + (cachedSecurityCode ? 'yes' : 'no'),
            '---',
            debugLogLines.join('\n')
        ].join('\n');
    }

    // 随机邮箱
    function randEmail() {
        var c = 'abcdefghijklmnopqrstuvwxyz0123456789', e = '';
        for (var i = 0; i < 17; i++) e += c[Math.floor(Math.random() * c.length)];
        return e + '@gmail.com';
    }

    // 随机密码
    function randPass() {
        var L = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ';
        var D = '0123456789', S = '!@#$%^', A = L + D + S;
        var p = L[Math.floor(Math.random()*26)] + L[26+Math.floor(Math.random()*26)] + D[Math.floor(Math.random()*10)] + S[Math.floor(Math.random()*6)];
        for (var i = 4; i < 14; i++) p += A[Math.floor(Math.random()*A.length)];
        return p.split('').sort(function(){return Math.random()-0.5}).join('');
    }

    // 填写input
    function fill(id, val) {
        var el = document.getElementById(id);
        if (!el) { log('未找到字段：' + id); return false; }
        fillElement(el, val);
        log('已填写字段：' + id + ' = ' + el.value);
        return true;
    }

    // 按选择器填写
    function fillSel(sel, val) {
        var el = document.querySelector(sel);
        if (!el) { log('未找到选择器：' + sel); return false; }
        fillElement(el, val);
        log('已填写选择器：' + sel + ' = ' + el.value);
        return true;
    }

    function fillElement(el, val) {
        var value = String(val == null ? '' : val);
        var proto = null;
        if (el instanceof HTMLInputElement) proto = HTMLInputElement.prototype;
        else if (el instanceof HTMLTextAreaElement) proto = HTMLTextAreaElement.prototype;
        else if (el instanceof HTMLSelectElement) proto = HTMLSelectElement.prototype;
        var desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
        if (desc && typeof desc.set === 'function') {
            desc.set.call(el, value);
        } else {
            el.value = value;
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
    }

    function applyRuntimeConfig() {
        var runtime = window.__PROTOCOL_REG_CHECKOUT_CONFIG__ || {};
        var sms = runtime.checkoutSms || {};
        var numbers = Array.isArray(sms.numbers) ? sms.numbers : [];
        CONFIG.smsNumbers = numbers.map(function(item) {
            return {
                phone: String(item.phone || item.number || '').trim(),
                smsUrl: String(item.smsUrl || item.sms_url || item.url || '').trim(),
                label: String(item.label || item.name || '').trim()
            };
        }).filter(function(item) {
            return item.phone && item.smsUrl;
        });
        CONFIG.smsTimeoutSeconds = Number(sms.timeoutSeconds || sms.timeout_seconds || sms.timeout || CONFIG.smsTimeoutSeconds) || CONFIG.smsTimeoutSeconds;
        CONFIG.smsPollSeconds = Number(sms.pollSeconds || sms.poll_seconds || sms.poll || CONFIG.smsPollSeconds) || CONFIG.smsPollSeconds;
        if (CONFIG.smsNumbers.length) {
            selectedSmsNumber = CONFIG.smsNumbers[0];
            CONFIG.phone = selectedSmsNumber.phone;
            log('当前：已加载 checkout 短信配置 ' + CONFIG.smsNumbers.length + ' 组，使用 ' + CONFIG.phone);
        } else {
            log('当前：未配置 checkout 短信收码，使用脚本默认手机号');
        }
    }

    function currentSmsNumber() {
        if (selectedSmsNumber && selectedSmsNumber.phone && selectedSmsNumber.smsUrl) {
            return selectedSmsNumber;
        }
        if (CONFIG.smsNumbers && CONFIG.smsNumbers.length) {
            selectedSmsNumber = CONFIG.smsNumbers[0];
            CONFIG.phone = selectedSmsNumber.phone;
            return selectedSmsNumber;
        }
        return {
            phone: CONFIG.phone,
            smsUrl: '',
            label: ''
        };
    }

    function configuredPhone() {
        return currentSmsNumber().phone || CONFIG.phone;
    }

    function usStateName(value) {
        var raw = String(value || '').trim();
        if (!raw) return US_FALLBACK_ADDRESS.state;
        var upper = raw.toUpperCase();
        if (US_STATE_ALIASES[upper]) return US_STATE_ALIASES[upper];
        for (var code in US_STATE_ALIASES) {
            if (Object.prototype.hasOwnProperty.call(US_STATE_ALIASES, code) &&
                US_STATE_ALIASES[code].toLowerCase() === raw.toLowerCase()) {
                return US_STATE_ALIASES[code];
            }
        }
        return US_FALLBACK_ADDRESS.state;
    }

    function usStateCode(value) {
        var raw = String(value || '').trim();
        if (!raw) return US_FALLBACK_ADDRESS.stateCode;
        var upper = raw.toUpperCase();
        if (US_STATE_ALIASES[upper]) return upper;
        for (var code in US_STATE_ALIASES) {
            if (Object.prototype.hasOwnProperty.call(US_STATE_ALIASES, code) &&
                US_STATE_ALIASES[code].toLowerCase() === raw.toLowerCase()) {
                return code;
            }
        }
        return US_FALLBACK_ADDRESS.stateCode;
    }

    function usZip(value) {
        var m = String(value || '').match(/\b(\d{5})(?:-\d{4})?\b/);
        return m ? m[1] : US_FALLBACK_ADDRESS.zip;
    }

    function firstText(values, fallback) {
        for (var i = 0; i < values.length; i++) {
            var text = String(values[i] || '').trim();
            if (text) return text;
        }
        return fallback;
    }

    function normalizeUsAddress(addr) {
        // PayPal 对 city/state/ZIP 的一致性校验很严，支付页固定使用一组确定有效的美国地址。
        return {
            street: US_FALLBACK_ADDRESS.street,
            city: US_FALLBACK_ADDRESS.city,
            state: usStateName(US_FALLBACK_ADDRESS.state),
            stateCode: usStateCode(US_FALLBACK_ADDRESS.stateCode),
            zip: usZip(US_FALLBACK_ADDRESS.zip)
        };
    }

    function ensureCountryUS(id) {
        var el = document.getElementById(id);
        if (!el) { log('未找到国家字段：' + id); return; }
        var before = el.value;
        fillSelect(id, 'US');
        if (el.value === before && String(el.value || '').toUpperCase() !== 'US') {
            el.value = 'US';
            el.dispatchEvent(new Event('change', { bubbles: true }));
            log('当前：国家字段已强制切到 US');
        }
    }

    // 填写下拉框
    function fillSelect(id, text) {
        var el = document.getElementById(id);
        if (!el) { log('未找到下拉框：' + id); return false; }
        if (!el.options || typeof el.options.length !== 'number') {
            log('字段不是下拉框：' + id);
            return false;
        }
        for (var i = 0; i < el.options.length; i++) {
            if (el.options[i].text.toLowerCase().includes(text.toLowerCase()) || el.options[i].value.toLowerCase().includes(text.toLowerCase())) {
                el.value = el.options[i].value;
                el.dispatchEvent(new Event('change', { bubbles: true }));
                log('已选择下拉框：' + id + ' = ' + el.options[i].text);
                return true;
            }
        }
        log('下拉框未匹配选项：' + id + ' / ' + text);
        return false;
    }

    function selectGoogleAddressFirst(inputSel, cb, retries) {
        retries = retries || 0;
        var input = document.querySelector(inputSel);
        if (!input) { log('未找到地址输入框：' + inputSel); cb && cb(false); return; }

        var first = document.querySelector('#suggestedAddressList button') ||
                    document.querySelector('#addressSuggestionContainer button') ||
                    document.querySelector('.pac-container .pac-item');
        if (first) {
            first.click();
            log('已点击谷歌地址候选第一条');
            setTimeout(function() { cb && cb(true); }, 500);
            return;
        }

        if (retries === 0) {
            input.focus();
            input.dispatchEvent(new Event('focus', { bubbles: true }));
        }

        if (retries < 12) {
            setTimeout(function() {
                selectGoogleAddressFirst(inputSel, cb, retries + 1);
            }, 250);
            return;
        }

        log('未找到谷歌地址候选，改用键盘回退');
        input.focus();
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', code: 'ArrowDown', keyCode: 40, which: 40, bubbles: true }));
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
        input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
        setTimeout(function() { cb && cb(false); }, 300);
    }

    function fillGoogleAddress(line1Sel, citySel, zipSel, stateId, addr, cb) {
        addr = normalizeUsAddress(addr);
        fillSel(line1Sel, addr.street);
        selectGoogleAddressFirst(line1Sel, function() {
            rewriteAddressFields(line1Sel, citySel, zipSel, stateId, addr, 0);
            setTimeout(function() { rewriteAddressFields(line1Sel, citySel, zipSel, stateId, addr, 1); }, 400);
            setTimeout(function() { rewriteAddressFields(line1Sel, citySel, zipSel, stateId, addr, 2); }, 1200);
            setTimeout(function() {
                rewriteAddressFields(line1Sel, citySel, zipSel, stateId, addr, 3);
                cb && cb();
            }, 2500);
        });
    }

    function rewriteAddressFields(line1Sel, citySel, zipSel, stateId, addr, pass) {
        fillSel(line1Sel, addr.street);
        fillSel(citySel, addr.city);
        fillSel(zipSel, addr.zip);
        fillState(stateId, addr);
        log('当前：已重写美国账单地址 #' + pass + ' ' + JSON.stringify(addr));
    }

    function fillState(stateId, addr) {
        var stateName = addr && addr.state ? addr.state : US_FALLBACK_ADDRESS.state;
        var stateCode = addr && addr.stateCode ? addr.stateCode : usStateCode(stateName);
        if (fillSelect(stateId, stateCode) || fillSelect(stateId, stateName)) {
            return true;
        }
        var el = document.getElementById(stateId);
        if (!el) {
            log('未找到州字段：' + stateId);
            return false;
        }
        fillElement(el, stateName);
        log('已填写州字段：' + stateId + ' = ' + stateName);
        return true;
    }

    function findFirst(selectors) {
        for (var i = 0; i < selectors.length; i++) {
            try {
                var el = document.querySelector(selectors[i]);
                if (el) return el;
            } catch (e) {}
        }
        return null;
    }

    function fieldText(el) {
        if (!el) return '';
        if (el instanceof HTMLSelectElement) {
            var option = el.options[el.selectedIndex];
            return String((el.value || '') + ' ' + (option ? option.text : '')).trim();
        }
        if ('value' in el) return String(el.value || '').trim();
        return String(el.textContent || '').trim();
    }

    function selectStateElement(el, addr) {
        if (!el) return false;
        var stateName = addr.state;
        var stateCode = addr.stateCode;
        if (el instanceof HTMLSelectElement) {
            for (var i = 0; i < el.options.length; i++) {
                var option = el.options[i];
                var value = String(option.value || '').trim();
                var text = String(option.text || '').trim();
                if (value.toUpperCase() === stateCode.toUpperCase() ||
                    text.toLowerCase() === stateName.toLowerCase() ||
                    value.toLowerCase() === stateName.toLowerCase()) {
                    el.value = option.value;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                    log('已选择州字段：' + text);
                    return true;
                }
            }
            return false;
        }
        if ('value' in el) {
            fillElement(el, stateName);
            return true;
        }
        el.click();
        setTimeout(function() {
            clickStateOption(stateName, stateCode);
        }, 150);
        return true;
    }

    function clickStateOption(stateName, stateCode) {
        var nodes = Array.prototype.slice.call(document.querySelectorAll('[role="option"], li, button, div'));
        for (var i = 0; i < nodes.length; i++) {
            var node = nodes[i];
            if (node.offsetParent === null) continue;
            var text = String(node.textContent || '').trim();
            if (!text) continue;
            if (text.toLowerCase() === stateName.toLowerCase() || text.toUpperCase() === stateCode.toUpperCase()) {
                node.click();
                log('已点击州候选：' + text);
                return true;
            }
        }
        return false;
    }

    function paypalAddressFields() {
        return {
            line1: findFirst([
                '#billingLine1',
                'input[name="billingLine1"]',
                'input[autocomplete="address-line1"]',
                'input[aria-label*="street" i]',
                'input[placeholder*="street" i]'
            ]),
            city: findFirst([
                '#billingCity',
                'input[name="billingCity"]',
                'input[autocomplete="address-level2"]',
                'input[aria-label*="city" i]',
                'input[placeholder*="city" i]'
            ]),
            state: findFirst([
                '#billingState',
                'select[name="billingState"]',
                'input[name="billingState"]',
                '[aria-label*="state" i]',
                '[placeholder*="state" i]'
            ]),
            zip: findFirst([
                '#billingPostalCode',
                'input[name="billingPostalCode"]',
                'input[autocomplete="postal-code"]',
                'input[aria-label*="zip" i]',
                'input[aria-label*="postal" i]',
                'input[placeholder*="zip" i]',
                'input[placeholder*="postal" i]'
            ])
        };
    }

    function isStateValue(addr, value) {
        var raw = String(value || '').trim().toLowerCase();
        if (!raw) return false;
        return raw.indexOf(String(addr.state || '').toLowerCase()) !== -1 ||
               raw.split(/\s+/).indexOf(String(addr.stateCode || '').toLowerCase()) !== -1 ||
               raw === String(addr.stateCode || '').toLowerCase();
    }

    function isPayPalBillingAddressValid() {
        var addr = normalizeUsAddress();
        var fields = paypalAddressFields();
        if (!fields.line1 || !fields.city || !fields.zip || !fields.state) return false;
        return fieldText(fields.line1).toLowerCase().indexOf(addr.street.toLowerCase()) !== -1 &&
               fieldText(fields.city).toLowerCase() === addr.city.toLowerCase() &&
               fieldText(fields.zip) === addr.zip &&
               isStateValue(addr, fieldText(fields.state));
    }

    function repairPayPalBillingAddress(reason) {
        if (!currentHost().includes('paypal.com') || !currentPath().includes('/checkoutweb/')) {
            return false;
        }
        var addr = normalizeUsAddress();
        var fields = paypalAddressFields();
        if (!fields.line1 && !fields.city && !fields.zip && !fields.state) {
            return false;
        }
        if (isPayPalBillingAddressValid()) {
            return true;
        }
        log('当前：检测到账单地址不合法，自动修复 ' + (reason || ''));
        if (fields.line1) fillElement(fields.line1, addr.street);
        if (fields.city) fillElement(fields.city, addr.city);
        if (fields.zip) fillElement(fields.zip, addr.zip);
        if (fields.state) selectStateElement(fields.state, addr);
        return true;
    }

    function startPayPalAddressWatcher() {
        if (!currentHost().includes('paypal.com')) {
            return;
        }
        var ticks = 0;
        setInterval(function() {
            ticks += 1;
            if (ticks > 120) return;
            repairPayPalBillingAddress('watcher #' + ticks);
        }, 1000);
        setTimeout(function() { repairPayPalBillingAddress('startup'); }, 500);
        setTimeout(function() { repairPayPalBillingAddress('startup-late'); }, 2500);
    }

    function httpRequest(opts) {
        if (typeof GM_xmlhttpRequest === 'function') {
            GM_xmlhttpRequest(opts);
            return;
        }
        if (typeof GM !== 'undefined' && GM && typeof GM.xmlHttpRequest === 'function') {
            GM.xmlHttpRequest(opts);
            return;
        }
        if (typeof fetch === 'function') {
            fetch(opts.url, {
                method: opts.method || 'GET',
                headers: opts.headers || {},
                body: opts.data
            }).then(function(res) {
                return res.text().then(function(text) {
                    if (opts.onload) {
                        opts.onload({
                            status: res.status,
                            statusText: res.statusText,
                            responseText: text
                        });
                    }
                });
            }).catch(function(err) {
                if (opts.onerror) {
                    opts.onerror({ statusText: err && err.message ? err.message : 'fetch failed' });
                }
            });
            return;
        }
        throw new Error('No supported HTTP request API available');
    }

    function getValue(id) {
        var el = document.getElementById(id);
        return el ? String(el.value || '').trim() : '';
    }

    function pageText() {
        return document.body ? String(document.body.innerText || '') : '';
    }

    function isPayPalSecurityFlowPage() {
        if (!currentHost().includes('paypal.com')) {
            return false;
        }
        if (window.location.href.includes('/webapps/hermes')) {
            return true;
        }
        return currentPath().includes('/checkoutweb/signup') && hasSecurityCodePromptContent();
    }

    function hasSecurityCodePromptContent() {
        var dialog = securityCodeDialog();
        var text = dialog ? String(dialog.innerText || '') : pageText();
        if (/enter your code|we sent a 6-digit code|6-digit code to|resend/i.test(text)) {
            return true;
        }
        var root = dialog || document;
        return !!root.querySelector('input[autocomplete="one-time-code"]') ||
               !!root.querySelector('input[name*="code" i]') ||
               !!root.querySelector('input[id*="code" i]');
    }

    function hasOpenAIBillingAddressForm() {
        return !!(document.getElementById('billingAddressLine1') &&
                  document.getElementById('billingCountry') &&
                  document.getElementById('billingAdministrativeArea'));
    }

    function isOpenAIAddressFilled() {
        return !!(getValue('billingAddressLine1') &&
                  getValue('billingLocality') &&
                  getValue('billingPostalCode') &&
                  getValue('billingAdministrativeArea'));
    }

    function isSignupAddressFilled() {
        if (currentHost().includes('paypal.com')) {
            return isPayPalBillingAddressValid();
        }
        return !!(getValue('billingLine1') &&
                  getValue('billingCity') &&
                  getValue('billingPostalCode') &&
                  getValue('billingState'));
    }

    function waitForOpenAIAddressFilled(cb, retries) {
        retries = retries || 0;
        if (isOpenAIAddressFilled()) {
            log('已确认 OpenAI 账单地址已填写');
            cb(true);
            return;
        }
        if (retries < 10) {
            setTimeout(function() {
                waitForOpenAIAddressFilled(cb, retries + 1);
            }, 500);
            return;
        }
        log('等待：OpenAI 账单地址尚未填写完整');
        cb(false);
    }

    function waitForSignupAddressFilled(cb, retries) {
        retries = retries || 0;
        if (isSignupAddressFilled()) {
            log('已确认 signup 页面账单地址已填写');
            cb(true);
            return;
        }
        if (retries < 10) {
            setTimeout(function() {
                waitForSignupAddressFilled(cb, retries + 1);
            }, 500);
            return;
        }
        log('等待：signup 页面地址还没确认填写完成');
        cb(false);
    }

    function ensureOpenAIPayPalAddressForm(cb, retries) {
        retries = retries || 0;
        if (hasOpenAIBillingAddressForm()) {
            log('已看到 PayPal 账单地址表单');
            cb(true);
            return;
        }

        var ppBtn = document.querySelector('[data-testid="paypal-accordion-item-button"]') ||
                    document.querySelector('.paypal-accordion-item button');
        if (ppBtn) {
            ppBtn.click();
            log('当前：正在点击 PayPal 按钮，等待账单地址表单出现');
        }

        if (retries < 8) {
            setTimeout(function() {
                ensureOpenAIPayPalAddressForm(cb, retries + 1);
            }, 1000);
            return;
        }

        log('等待结束：PayPal 账单地址表单仍未出现');
        cb(false);
    }

    var cachedSecurityCode = null;

    function getSecurityCode(cb, retries) {
        retries = retries || 0;
        if (cachedSecurityCode) {
            log('当前：复用缓存的短信验证码');
            cb(cachedSecurityCode);
            return;
        }
        if (retries === 0 && smsPollingActive) {
            log('当前：短信接口已有轮询任务，跳过重复启动');
            return;
        }
        smsPollingActive = true;

        var smsConfig = currentSmsNumber();
        var paypalSmsApiUrl = smsConfig.smsUrl || '';
        if (!paypalSmsApiUrl) {
            smsPollingActive = false;
            log('未配置短信收码 URL，无法自动获取验证码');
            cb(null);
            return;
        }
        log('当前：正在轮询短信接口 ' + maskUrl(paypalSmsApiUrl));
        var smsUrl = paypalSmsApiUrl + (paypalSmsApiUrl.indexOf('?') === -1 ? '?' : '&') + '_ts=' + Date.now();

        httpRequest({
            method: 'GET',
            url: smsUrl,
            headers: {
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'accept-language': 'zh,en;q=0.9,zh-CN;q=0.8',
                'cache-control': 'max-age=0, no-cache',
                'pragma': 'no-cache'
            },
            onload: function(r) {
                try {
                    var raw = extractSmsText(r.responseText);
                    var m = String(raw).match(/\b(\d{6})\b/);
                    if (m) {
                        smsPollingActive = false;
                        cachedSecurityCode = m[1];
                        log('已获取短信验证码：' + cachedSecurityCode);
                        cb(cachedSecurityCode);
                        return;
                    }
                    log('短信接口当前没有 6 位验证码：' + maskSmsText(raw));
                } catch (e) {
                    log('短信验证码解析失败：' + e.message);
                }

                if (shouldPollSmsAgain(retries)) {
                    log('等待：' + CONFIG.smsPollSeconds + ' 秒后继续轮询短信验证码 #' + (retries + 1));
                    setTimeout(function() { getSecurityCode(cb, retries + 1); }, CONFIG.smsPollSeconds * 1000);
                    return;
                }
                smsPollingActive = false;
                cb(null);
            },
            onerror: function(e) {
                log('短信验证码请求失败：' + (e.statusText || 'network error'));
                if (shouldPollSmsAgain(retries)) {
                    log('等待：' + CONFIG.smsPollSeconds + ' 秒后继续轮询短信验证码 #' + (retries + 1));
                    setTimeout(function() { getSecurityCode(cb, retries + 1); }, CONFIG.smsPollSeconds * 1000);
                    return;
                }
                smsPollingActive = false;
                cb(null);
            }
        });
    }

    function maskUrl(url) {
        return String(url || '').replace(/([?&](?:token|key|api_key|apikey)=)[^&]+/ig, '$1***');
    }

    function maskSmsText(text) {
        return String(text || '')
            .replace(/\b\d{6}\b/g, '******')
            .replace(/\b\d{10,}\b/g, function(m) { return m.slice(0, 3) + '***' + m.slice(-2); })
            .slice(0, 500);
    }

    function extractSmsText(responseText) {
        var text = String(responseText || '');
        try {
            var d = JSON.parse(text);
            return d.data || d.message || d.msg || d.sms || d.text || d.content || text;
        } catch (e) {
            return text;
        }
    }

    function shouldPollSmsAgain(retries) {
        var maxRetries = Math.ceil((Number(CONFIG.smsTimeoutSeconds) || 180) / (Number(CONFIG.smsPollSeconds) || 2));
        return retries < Math.max(1, maxRetries);
    }

    function hasSecurityCodePrompt() {
        if (!isPayPalSecurityFlowPage()) {
            return false;
        }
        return hasSecurityCodePromptContent();
    }

    function handleSecurityCodePrompt() {
        if (!currentHost().includes('paypal.com')) {
            return false;
        }
        if (!hasSecurityCodePrompt()) {
            return false;
        }
        if (securityAutoStopped) {
            log('当前：自动验证码输入已停止，请手动输入缓存验证码');
            return true;
        }
        if (securityCodeHandling) {
            if (cachedSecurityCode) {
                retryFillCachedSecurityCode('handling-loop');
            }
            log('当前：短信验证码正在自动处理中');
            return true;
        }
        securityCodeHandling = true;
        log('当前：已进入短信验证码页面，准备自动获取并填写验证码');
        getSecurityCode(function(code) {
            if (!code) {
                securityCodeHandling = false;
                log('未能自动获取短信验证码，请手动填写验证码');
                waitForSecurityCodeDismiss();
                return;
            }
            if (!fillSecurityCode(code)) {
                securityCodeHandling = false;
                log('验证码输入框未找到，请手动填写验证码：' + code);
                waitForSecurityCodeDismiss();
                return;
            }
            securityFillAttempts += 1;
            lastSecurityFillAt = Date.now();
            setTimeout(function() {
                clickSecurityCodeSubmitButtonIfReady();
                waitForSecurityCodeDismiss();
            }, 1200);
        });
        return true;
    }

    function retryFillCachedSecurityCode(reason) {
        if (!cachedSecurityCode || !hasSecurityCodePrompt()) {
            return false;
        }
        if (securityAutoStopped) {
            return false;
        }
        if (securityFillAttempts >= 3 && !isSecurityCodeVisiblyFilled()) {
            securityAutoStopped = true;
            securityCodeHandling = false;
            log('当前：自动输入验证码连续失败，已停止自动重试。请手动输入验证码：' + cachedSecurityCode);
            return false;
        }
        var now = Date.now();
        if (now - lastSecurityFillAt < 1200) {
            return false;
        }
        securityFillAttempts += 1;
        lastSecurityFillAt = now;
        log('当前：使用缓存验证码重试填入 #' + securityFillAttempts + ' reason=' + reason);
        if (!fillSecurityCode(cachedSecurityCode)) {
            log('缓存验证码重填失败：未找到输入框');
            return false;
        }
        setTimeout(function() {
            clickSecurityCodeSubmitButtonIfReady();
        }, 1200);
        return true;
    }

    function startSecurityCodeWatcher() {
        if (!currentHost().includes('paypal.com')) {
            return;
        }
        log('当前：短信验证码 watcher 已启动');
        setInterval(function() {
            securityWatcherTicks += 1;
            if (securityWatcherTicks % 5 === 0) {
                log('心跳：验证码弹窗=' + hasSecurityCodePromptContent() + ' 处理中=' + securityCodeHandling);
            } else {
                renderDebugPanel();
            }
            if (cachedSecurityCode && hasSecurityCodePromptContent()) {
                retryFillCachedSecurityCode('watcher');
            }
            handleSecurityCodePrompt();
        }, 1000);
        setTimeout(function() {
            handleSecurityCodePrompt();
        }, 250);
    }

    function waitForSecurityCodeDismiss(retries) {
        retries = retries || 0;
        if (!hasSecurityCodePrompt()) {
            securityCodeHandling = false;
            log('当前：验证码弹窗已关闭，继续后续流程');
            setTimeout(function() { clickBtnWithRetry(); }, 1000);
            return;
        }
        if (retries < 120) {
            setTimeout(function() { waitForSecurityCodeDismiss(retries + 1); }, 1000);
            return;
        }
        log('等待结束：验证码弹窗长时间未关闭');
    }

    function clickSecurityCodeSubmitButton() {
        var root = securityCodeDialog() || document;
        var buttons = Array.prototype.slice.call(root.querySelectorAll('button')).filter(function(btn) {
            return btn.offsetParent !== null && !btn.disabled;
        });
        for (var i = 0; i < buttons.length; i++) {
            var text = String(buttons[i].textContent || '').trim();
            if (/^(continue|submit|verify|confirm|next|done|继续|提交|验证|确认|下一步)$/i.test(text)) {
                log('当前：正在点击验证码提交按钮 ' + text);
                buttons[i].click();
                return true;
            }
        }
        var active = document.activeElement;
        if (active) {
            active.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
            active.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
            log('当前：未找到验证码提交按钮，已发送 Enter');
            return true;
        }
        return clickBtnWithRetry();
    }

    function clickSecurityCodeSubmitButtonIfReady() {
        if (!isSecurityCodeVisiblyFilled()) {
            log('当前：验证码尚未写入页面，暂不提交');
            return false;
        }
        return clickSecurityCodeSubmitButton();
    }

    function isSecurityCodeVisiblyFilled() {
        var inputs = securityCodeInputs();
        if (inputs.length === 1) {
            return /^\d{6}$/.test(String(inputs[0].value || '').trim());
        }
        if (inputs.length >= 6) {
            var code = inputs.slice(0, 6).map(function(el) {
                return String(el.value || '').trim();
            }).join('');
            return /^\d{6}$/.test(code);
        }
        return false;
    }

    function securityCodeInputs() {
        var root = securityCodeDialog();
        if (!root) {
            log('未找到验证码弹窗容器');
            return [];
        }
        var inputs = Array.prototype.slice.call(root.querySelectorAll('input')).filter(function(el) {
            return el.offsetParent !== null && !el.disabled && !el.readOnly;
        });
        var single = inputs.filter(function(el) {
            var id = (el.id || '') + ' ' + (el.name || '') + ' ' + (el.autocomplete || '') + ' ' + (el.getAttribute('aria-label') || '');
            return /code|otp|security|verify|one-time/i.test(id) ||
                   el.autocomplete === 'one-time-code' ||
                   (el.maxLength && el.maxLength === 6);
        });
        if (single.length) return [single[0]];
        return inputs.filter(function(el) {
            var rect = el.getBoundingClientRect();
            var id = el.id || '';
            var name = el.name || '';
            var meta = id + ' ' + name + ' ' + (el.autocomplete || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.placeholder || '');
            return (el.maxLength === 1 && /otp|code|security|verify|one-time/i.test(meta)) ||
                   /^ci-ciBasic-\d+$/.test(id) ||
                   /^ciBasic-\d+$/.test(name) ||
                   /^\d-6$/.test(el.getAttribute('aria-label') || '') ||
                   (el.maxLength === 1 && hasSecurityCodePromptContent()) ||
                   (hasSecurityCodePromptContent() && rect.width > 20 && rect.width < 80 && rect.height > 20 && rect.height < 80);
        }).sort(function(a, b) {
            var ar = a.getBoundingClientRect();
            var br = b.getBoundingClientRect();
            if (Math.abs(ar.top - br.top) > 8) return ar.top - br.top;
            if (Math.abs(ar.left - br.left) > 4) return ar.left - br.left;
            return 0;
        });
    }

    function securityCodeDialog() {
        var candidates = Array.prototype.slice.call(document.querySelectorAll(
            '[role="dialog"], [aria-modal="true"], .modal, .vx_modal, div'
        )).filter(function(el) {
            if (el === debugPanelNode || !el || el.offsetParent === null) return false;
            var text = String(el.innerText || '');
            if (!/enter your code|we sent a 6-digit code|6-digit code to|resend/i.test(text)) return false;
            return el.querySelectorAll && el.querySelectorAll('input').length > 0;
        }).sort(function(a, b) {
            var ar = a.getBoundingClientRect();
            var br = b.getBoundingClientRect();
            var aa = Math.max(1, ar.width * ar.height);
            var ba = Math.max(1, br.width * br.height);
            return aa - ba;
        });
        if (candidates.length) {
            return candidates[0];
        }
        return null;
    }

    function fillSecurityCode(code) {
        var inputs = securityCodeInputs();
        log('当前：可见验证码候选输入框数量 ' + inputs.length);

        if (inputs.length === 1) {
            typeOtpCode([inputs[0]], code);
            log('已填写短信验证码：单输入框');
            return true;
        }

        if (inputs.length >= 6) {
            typeOtpCode(inputs.slice(0, 6), code);
            log('已填写短信验证码：分割输入框，数量=' + inputs.length);
            return true;
        }

        log('未找到短信验证码输入框，候选数量：' + inputs.length);
        return false;
    }

    function typeOtpCode(inputs, code) {
        var digits = String(code || '').split('');
        if (!inputs.length || !digits.length) {
            return;
        }
        if (inputs.length === 1) {
            try { inputs[0].focus(); inputs[0].click(); } catch (e) {}
            fillOtpInput(inputs[0], String(code || ''));
            return;
        }
        log('当前：开始逐位写入验证码');
        inputs[0].focus();
        inputs[0].click();
        digits.slice(0, inputs.length).forEach(function(ch, index) {
            setTimeout(function() {
                var target = inputs[index];
                fillOtpInput(target, ch);
                var next = inputs[index + 1];
                if (next) next.focus();
                log('当前：已写入验证码位 #' + (index + 1));
            }, index * 80);
        });
    }

    function dispatchBeforeInput(el, value) {
        try {
            el.dispatchEvent(new InputEvent('beforeinput', {
                bubbles: true,
                cancelable: true,
                inputType: 'insertText',
                data: value
            }));
        } catch (e) {}
    }

    function dispatchInput(el, value) {
        try {
            el.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                inputType: 'insertText',
                data: value
            }));
        } catch (e) {
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function fillOtpInput(el, val) {
        var value = String(val == null ? '' : val);
        try { el.focus(); } catch (e) {}
        try { el.click(); } catch (e) {}
        dispatchBeforeInput(el, value);
        fillElement(el, value);
        dispatchInput(el, value);
        dispatchKey(el, 'keyup', value.slice(-1) || value);
    }

    function dispatchKey(el, type, key) {
        var code = /^\d$/.test(key) ? 48 + Number(key) : 0;
        try {
            el.dispatchEvent(new KeyboardEvent(type, {
                key: key,
                code: /^\d$/.test(key) ? 'Digit' + key : key,
                keyCode: code,
                which: code,
                bubbles: true,
                cancelable: true
            }));
        } catch (e) {}
    }

    function clickConsentButton(retries) {
        retries = retries || 0;
        var btn = document.getElementById('consentButton') ||
                  document.querySelector('button[data-testid="consentButton"]');
        if (btn) {
            if (btn.disabled) {
                log('等待：同意按钮暂时不可用');
                if (retries < 20) setTimeout(function() { clickConsentButton(retries + 1); }, 500);
                return;
            }
            log('当前：正在点击同意按钮');
            btn.click();
            return;
        }
        if (retries < 20) {
            setTimeout(function() { clickConsentButton(retries + 1); }, 500);
            return;
        }
        log('未找到同意按钮');
    }

    function fallbackProfile() {
        return {
            address: normalizeUsAddress(US_FALLBACK_ADDRESS),
            card: {
                number: CONFIG.cardNumber,
                expiry: CONFIG.cardExpiry,
                cvv: CONFIG.cardCvv
            }
        };
    }

    // 从meiguodizhi.com API获取地址和卡信息
    function getProfile(cb) {
        if (cachedProfile) {
            log('当前：复用缓存的地址和卡信息');
            return cb(cachedProfile);
        }
        log('当前：正在从 meiguodizhi.com API 获取地址和卡信息');
        httpRequest({
            method: 'POST',
            url: 'https://www.meiguodizhi.com/api/v1/dz',
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify({ path: '/', method: 'address' }),
            onload: function(r) {
                try {
                    var d = JSON.parse(r.responseText);
                    var a = d.address || d;
                    var profile = {
                        address: normalizeUsAddress(a),
                        card: {
                            number: a.Credit_Card_Number || d.Credit_Card_Number || CONFIG.cardNumber,
                            expiry: a.Expires || a.Expiry || d.Expires || CONFIG.cardExpiry,
                            cvv: a.CVV2 || a.CVV || d.CVV2 || CONFIG.cardCvv
                        }
                    };
                    cachedProfile = profile;
                    log('已从 API 获取资料：' + JSON.stringify(profile));
                    cb(profile);
                } catch(e) {
                    log('资料解析失败：' + e.message);
                    cachedProfile = fallbackProfile();
                    cb(cachedProfile);
                }
            },
            onerror: function(e) {
                log('资料请求失败：' + (e.statusText || 'network error'));
                cachedProfile = fallbackProfile();
                cb(cachedProfile);
            }
        });
    }

    // 点击按钮（带重试）
    function clickBtn(retries) {
        retries = retries || 0;
        var btn = document.querySelector('button[data-testid="submit-button"]') ||
                  document.querySelector('button[data-testid="hosted-payment-submit-button"]') ||
                  document.querySelector('button[data-atomic-wait-intent="Submit_Email"]') ||
                  document.querySelector('button.SubmitButton--complete');
        if (!btn) {
            var all = document.querySelectorAll('button');
            for (var i = 0; i < all.length; i++) {
                var t = all[i].textContent.trim();
                if (t === '下一页' || t === 'Next' || t === 'Subscribe' || t === 'Pay' || t === 'Continue' || t === 'Agree' || t === 'Create an Account' || t === 'Create Account' || t === 'Agree & Create Account' || t === 'Agree and Create Account' || t === 'Agree & Continue') {
                    btn = all[i]; break;
                }
            }
        }
        if (btn) {
            if (btn.disabled) {
                log('等待：提交按钮暂时不可用');
                if (retries < 10) setTimeout(function() { clickBtn(retries + 1); }, 1000);
                return false;
            }
            var rect = btn.getBoundingClientRect();
            log('已找到按钮：' + btn.textContent.trim() + '，可见：' + (rect.height > 0));
            if (rect.height === 0) {
                log('等待：按钮当前不可见，准备重试');
                if (retries < 10) setTimeout(function() { clickBtn(retries + 1); }, 1000);
                return false;
            }
            log('当前：正在点击 ' + btn.textContent.trim());
            btn.click();
            return true;
        } else {
            log('等待：未找到提交按钮，准备重试（' + retries + '）');
            if (retries < 10) setTimeout(function() { clickBtn(retries + 1); }, 1000);
            return false;
        }
    }

    function findButtonByTexts(texts) {
        var buttons = document.querySelectorAll('button');
        for (var i = 0; i < buttons.length; i++) {
            var text = buttons[i].textContent.trim();
            for (var j = 0; j < texts.length; j++) {
                if (text === texts[j]) {
                    return buttons[i];
                }
            }
        }
        return null;
    }

    function clickElement(el, label) {
        if (!el) {
            return false;
        }
        var text = (label || el.textContent || '').trim();
        var rect = el.getBoundingClientRect();
        if (rect.height === 0) {
            log('等待：按钮不可见 ' + text);
            return false;
        }
        if (el.disabled) {
            log('等待：按钮不可用 ' + text);
            return false;
        }
        if (/creating|processing|loading|正在处理|创建中/i.test(text)) {
            log('等待：按钮处于处理中状态 ' + text);
            return false;
        }
        log('当前：正在点击 ' + text);
        el.click();
        return true;
    }

    function handlePayEntryFlow(retries) {
        retries = retries || 0;

        if (currentPath().includes('/checkoutweb/signup')) {
            log('当前：已进入 signup 页面');
            return true;
        }

        if (handleSecurityCodePrompt()) {
            return true;
        }

        var createBtn = document.querySelector('button[data-atomic-wait-intent="Pay_With_Card"]') ||
                        findButtonByTexts(['Create an Account', 'Create Account']);
        if (createBtn) {
            if (clickElement(createBtn, createBtn.textContent.trim() || 'Create an Account')) {
                if (retries < 12) {
                    setTimeout(function() { handlePayEntryFlow(retries + 1); }, 1500);
                }
                return true;
            }
        }

        var continueBtn = document.querySelector('button[data-testid="continueButton"]') ||
                          findButtonByTexts(['Continue to Payment']);
        var emailInput = document.getElementById('login_email') ||
                         document.getElementById('email') ||
                         document.querySelector('input[type="email"]');
        if (continueBtn) {
            if (emailInput && !String(emailInput.value || '').trim()) {
                var email = randEmail();
                fillElement(emailInput, email);
                log('当前：已填写 PayPal 入口邮箱 ' + email);
            }
            if (clickElement(continueBtn, continueBtn.textContent.trim() || 'Continue to Payment')) {
                if (retries < 12) {
                    setTimeout(function() { handlePayEntryFlow(retries + 1); }, 2000);
                }
                return true;
            }
        }

        if (retries < 12) {
            log('等待：/pay 页面下一步按钮或邮箱输入框尚未就绪');
            setTimeout(function() { handlePayEntryFlow(retries + 1); }, 1500);
            return true;
        }

        log('结束：/pay 页面流程未能继续推进');
        return false;
    }

    function clickBtnWithRetry(originUrl, retries) {
        retries = retries || 0;
        if (currentHost().includes('paypal.com') && /^\/pay\/?$/.test(currentPath())) {
            return handlePayEntryFlow(retries);
        }
        repairPayPalBillingAddress('before submit');
        var before = originUrl || window.location.href;
        var clicked = clickBtn();
        if (!clicked) {
            if (retries < 10) {
                setTimeout(function() { clickBtnWithRetry(before, retries + 1); }, 2000);
            }
            return;
        }

        setTimeout(function() {
            if (handleSecurityCodePrompt()) {
                return;
            }
            var stillOnSamePage = window.location.href === before;
            if (stillOnSamePage && retries < 10) {
                log('等待：提交后仍停留在当前页面，2 秒后重试');
                clickBtnWithRetry(before, retries + 1);
            }
        }, 2000);
    }

    function currentHost() {
        return (window.location && window.location.host) || '';
    }

    function currentPath() {
        return (window.location && window.location.pathname) || '';
    }

    // ========== 主逻辑 ==========
    log('当前页面：Host=' + currentHost() + ' Path=' + currentPath());
    startSecurityCodeWatcher();
    startPayPalAddressWatcher();

    // OpenAI/Stripe页面
    if (currentHost().includes('pay.openai.com') || currentHost().includes('checkout.stripe.com')) {
        log('当前流程：OpenAI / Stripe 页面');
        setTimeout(function() {
            ensureOpenAIPayPalAddressForm(function(ready) {
                if (!ready) {
                    return;
                }
                getProfile(function(profile) {
                    var addr = profile.address;
                    log('当前：准备填写账单地址 ' + JSON.stringify(addr));
                    ensureCountryUS('billingCountry');
                    fillGoogleAddress('#billingAddressLine1', '#billingLocality', '#billingPostalCode', 'billingAdministrativeArea', addr, function() {
                        waitForOpenAIAddressFilled(function(filled) {
                            if (!filled) {
                                return;
                            }
                            var cb = document.getElementById('termsOfServiceConsentCheckbox');
                            if (cb && !cb.checked) { cb.click(); log('当前：已勾选同意条款'); }
                            setTimeout(function() { clickBtnWithRetry(); }, 1000);
                        });
                    });
                });
            });
        }, 2000);
        return;
    }

    // PayPal登录页 /pay
    if (currentHost().includes('paypal.com') && /^\/pay\/?$/.test(currentPath())) {
        log('当前流程：PayPal /pay 页面，准备点击创建新账号');
        setTimeout(function() {
            handlePayEntryFlow();
        }, 2000);
        return;
    }

    // PayPal验证码页 /webapps/hermes
    if (currentHost().includes('paypal.com') && window.location.href.includes('/webapps/hermes')) {
        if (window.location.href.includes('/billingweb/review')) {
            log('当前流程：PayPal review 页面，准备点同意按钮');
            setTimeout(function() {
                clickConsentButton();
            }, 1000);
            return;
        }

        log('当前流程：PayPal 短信验证码页面，准备拉码');
        setTimeout(function() {
            handleSecurityCodePrompt();
        }, 1500);
        return;
    }

    // PayPal结账页 /checkoutweb
    if (currentHost().includes('paypal.com') && currentPath().includes('/checkoutweb/signup')) {
        log('当前流程：PayPal signup 页面，准备填写地址和卡信息');
        setTimeout(function() {
            var country = document.getElementById('billingCountry');
            if (country && country.value !== 'US') {
                ensureCountryUS('billingCountry');
            }
            getProfile(function(profile) {
                var addr = profile.address;
                var card = profile.card;
                fill('email', randEmail());
                fill('phone', configuredPhone());
                fill('cardNumber', card.number);
                fill('cardExpiry', card.expiry);
                fill('cardCvv', card.cvv);
                fill('password', randPass());
                fill('firstName', 'James');
                fill('lastName', 'Smith');
                fillGoogleAddress('#billingLine1', '#billingCity', '#billingPostalCode', 'billingState', addr, function() {
                    waitForSignupAddressFilled(function(filled) {
                        if (!filled) {
                            return;
                        }
                        setTimeout(function() { clickBtnWithRetry(); }, 500);
                    });
                });
            });
        }, 2000);
        return;
    }

    if (currentHost().includes('paypal.com') && currentPath().includes('/checkoutweb/')) {
        log('当前流程：PayPal checkout 页面，准备填写表单');
        setTimeout(function() {
            var country = document.getElementById('country');
            if (country && country.value !== 'US') {
                ensureCountryUS('country');
                log('当前：国家已切到 US，等待表单刷新');
                setTimeout(doFill, 3000);
            } else {
                doFill();
            }
        }, 2000);

        function doFill() {
            getProfile(function(profile) {
                var addr = profile.address;
                var card = profile.card;
                var email = randEmail();
                var password = randPass();
                log('当前：已生成邮箱和密码，准备填写');
                fill('email', email);
                fill('phone', configuredPhone());
                fill('cardNumber', card.number);
                fill('cardExpiry', card.expiry);
                fill('cardCvv', card.cvv);
                fill('password', password);
                fill('firstName', 'James');
                fill('lastName', 'Smith');
                fillGoogleAddress('#billingLine1', '#billingCity', '#billingPostalCode', 'billingState', addr, function() {
                    setTimeout(function() { clickBtnWithRetry(); }, 500);
                });
            });
        }
        return;
    }

    log('当前页面：未匹配到处理分支');
})();
