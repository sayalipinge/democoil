# How to Deploy JSW Coil OCR

Workers access via: https://jsw-coil-ocr.fly.dev
Works on any WiFi, any mobile data, anywhere.

---

## One-time setup (do this once)

### Step 1 — Install flyctl (Fly.io command line tool)
Open PowerShell and run:
```
powershell -Command "iwr https://fly.io/install.ps1 -useb | iex"
```

### Step 2 — Create free Fly.io account
```
fly auth signup
```
(opens browser — sign up with Google or email, free, no credit card)

### Step 3 — Go to project folder
```
cd C:\Users\sayal\Downloads\democoil\jsw_coil_ocr
```

### Step 4 — Create the app on Fly.io (first time only)
```
fly apps create jsw-coil-ocr
```

### Step 5 — Create persistent storage volume (first time only)
```
fly volumes create coil_data --region sin --size 3
```

### Step 6 — Set your Gemini API keys
```
fly secrets set GEMINI_API_KEY_1=your_first_key_here
fly secrets set GEMINI_API_KEY_2=your_second_key_here
fly secrets set GEMINI_API_KEY_3=your_third_key_here
```
Get free keys at: https://aistudio.google.com/apikey

### Step 7 — Deploy
```
fly deploy
```
Wait ~3 minutes. Done.

### Step 8 — Check it works
```
fly open
```
Opens https://jsw-coil-ocr.fly.dev in browser.

---

## Worker setup (tell each worker once)

1. Open Chrome on phone
2. Go to: https://jsw-coil-ocr.fly.dev
3. Chrome shows "Add to Home Screen" banner → tap it
4. App icon appears on home screen
5. Done — tap icon to open, works like a real app

---

## Update the app (when you make code changes)

Just run:
```
cd C:\Users\sayal\Downloads\democoil\jsw_coil_ocr
fly deploy
```
Takes ~2 minutes. Workers don't need to do anything — they reload the page.

---

## Add or change API keys later

```
fly secrets set GEMINI_API_KEY_1=new_key_here
```
App restarts automatically with new key.

---

## Check logs (if something breaks)

```
fly logs
```

## Check app status

```
fly status
```

---

## Cost

Fly.io free tier covers this app:
- 1 shared CPU + 512MB RAM machine = FREE
- 3GB persistent storage = FREE
- Bandwidth = FREE up to limits
- HTTPS = FREE

Total cost: ₹0/month (unless you exceed free limits, which won't happen for JSW scale)
