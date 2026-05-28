# How to Deploy JSW Coil OCR (Render.com — free, no credit card)

Workers will access via a URL like: **https://jsw-coil-ocr.onrender.com**
Works on any WiFi, any mobile data, anywhere. HTTPS = phone camera works.

The code already lives on GitHub:
**https://github.com/sayalipinge/democoil**  → branch **`deploy`**
(That `deploy` branch is a clean 65 KB copy — only the files needed to run.
 The big model/dataset files are NOT on it, so it builds fast and pushes fast.)

---

## PART A — Deploy on Render (one time, ~5 minutes)

> If JSW WiFi times out on github.com pages, do Part A from your **phone hotspot**
> instead. Render only needs GitHub during this one-time setup. After that the app
> lives on `onrender.com`, which is not blocked.

### Step 1 — Make a Render account
Go to https://render.com → **Get Started** → sign up with email (or "Sign in with GitHub").
Free. No credit card.

### Step 2 — New Blueprint
- Click **New +** (top right) → **Blueprint**.
- Connect your GitHub, pick repo **`sayalipinge/democoil`**.
- When it asks for the branch, choose **`deploy`** (NOT main / feature/fdb-detector).
- Render reads `render.yaml` automatically and shows a service named `jsw-coil-ocr`.
- Click **Apply** / **Create**.

### Step 3 — Add your Gemini API key
Render will ask for the secret `GEMINI_API_KEY_1` (because render.yaml marks it
`sync: false`). Paste your key.
- Get a free key at: https://aistudio.google.com/apikey
- (Optional, for 3x the free daily quota: also add `GEMINI_API_KEY_2`,
  `GEMINI_API_KEY_3` later under the service's **Environment** tab.)

### Step 4 — Wait for build
Render builds the Dockerfile (~3-4 min the first time). When it says **Live**,
click the URL at the top (e.g. `https://jsw-coil-ocr.onrender.com`).
Open it on your phone — you should see the camera scanner.

That's it. The app is now on the public internet.

---

## PART B — Tell each worker once

1. Open **Chrome** on the phone.
2. Go to the app URL (e.g. `https://jsw-coil-ocr.onrender.com`).
3. Chrome menu (three dots) → **Add to Home screen** → confirm.
4. An app icon appears. Tap it — opens full-screen like a real app.

---

## PART C — Update the app after you change code

The app runs from the **`deploy`** branch. To ship a change:

```powershell
# 1. edit files in C:\Users\sayal\Downloads\democoil\jsw_coil_ocr\ as usual
# 2. copy the changed file into the clean deploy repo, e.g.:
Copy-Item C:\Users\sayal\Downloads\democoil\jsw_coil_ocr\api\app.py `
          C:\Users\sayal\Downloads\coil_deploy\api\app.py
# 3. commit + push
cd C:\Users\sayal\Downloads\coil_deploy
git add -A
git commit -m "describe your change"
git push
```
Render auto-detects the push and redeploys in ~2 min. Workers just reload the page.

> Why a separate `coil_deploy` folder? Your main repo's history is 3.55 GB
> (model .pt files + dataset zips). Pushing that times out on JSW WiFi (HTTP 408).
> `coil_deploy` is a clean 65 KB git repo pointed at the same GitHub, branch `deploy`.
> Always commit deploy changes from `coil_deploy`, not the big repo.

---

## PART D — Free tier behaviour (important)

Render free web services **sleep after 15 min of no traffic**. The next scan
**wakes it (~50 sec cold start)**, then it's fast again.
- Fine for trials and light use.
- To make it always-on later: Render dashboard → the service → **upgrade to
  Starter ($7/mo)**. No code change needed. (Or move to a paid host.)

Also: free tier disk is **ephemeral** — uploaded images and the duplicate-check
list reset whenever Render restarts/redeploys. OCR still works fully; only the
2-month image storage needs a paid persistent disk (deferred for now).

---

## PART E — If something breaks

- **Build failed:** Render dashboard → service → **Logs** tab. Read the red lines.
  Most common: a typo in `requirements.txt` or `Dockerfile`.
- **App loads but every scan says "no API key":** the `GEMINI_API_KEY_1` env var
  isn't set. Dashboard → **Environment** → add it → service redeploys.
- **Camera doesn't open:** make sure the URL is **https://** (Render gives https
  automatically) and you allowed camera permission in Chrome.
- **"quota exhausted" on scans:** your Gemini free quota (1500 scans/key/day) ran
  out. Add `GEMINI_API_KEY_2` / `_3`, or wait for the daily reset. Workers can
  always type the ID manually in the meantime.

---

## Cost summary

| Item | Free tier | Cost |
|---|---|---|
| Render web service | yes (sleeps when idle) | ₹0 |
| Gemini 2.5 Flash | 1500 scans/day/key | ~₹10-50/mo at JSW volume |
| HTTPS | included | ₹0 |
| Persistent image storage | needs paid disk | deferred |

**Today's running cost: ₹0** (plus tiny Gemini usage).
