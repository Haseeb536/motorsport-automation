# Pushing to GitHub

Your public copy lives in this folder only — your local `motorsport` workspace is untouched.

## 1. Create the repo on GitHub

1. Go to [github.com/Haseeb536](https://github.com/Haseeb536?tab=repositories)
2. Click **New repository**
3. Name it: `motorsport-automation` (or any name you prefer)
4. Leave it **empty** (no README / .gitignore — this folder already has them)
5. Create the repository

## 2. Push from this folder

```powershell
cd C:\Users\hasee\Downloads\motorsport-github

git remote add origin https://github.com/Haseeb536/motorsport-automation.git
git branch -M main
git push -u origin main
```

## 3. Before you push — checklist

- [ ] `credentials.json` is **not** in this folder (only `credentials.json.example`)
- [ ] `.env` is **not** committed (only `.env.example`)
- [ ] No CSV / XLSX / log files added

## 4. After cloning elsewhere

```powershell
copy .env.example .env
# fill in .env
copy credentials.json.example credentials.json
# replace with your real Google service account JSON
pip install -r requirements.txt
```
