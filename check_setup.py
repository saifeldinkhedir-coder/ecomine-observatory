"""
EcoMine Observatory — فحص البيئة / Environment Doctor
=======================================================

شغّل هذا الملف أولًا، قبل أي شيء آخر.
Run this before anything else.

يفحص كل متطلب بالترتيب ويتوقف عند أول عطل، مع رسالة تشرح ماذا تفعل بالضبط.
لا يحتاج إلى أي ملف آخر من المشروع.

    python check_setup.py

Author: Seifeldin M.G. Alkhedir · Licence: GPL-3.0
"""

import sys

# معرّف المشروع يُقرأ من ملف .env أو من متغيّر البيئة — لا يُكتب هنا
# Project ID is read from .env or the environment, never hardcoded, so that a
# personal Cloud project ID is not published when this repo goes public.
import os
from pathlib import Path


def _load_project_id() -> str:
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("EE_PROJECT="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return os.environ.get("EE_PROJECT", "")


EE_PROJECT = _load_project_id()


GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


def ok(msg):
    print(f"  {GREEN}[PASS]{RESET} {msg}")


def fail(msg, fix):
    print(f"  {RED}[FAIL]{RESET} {msg}")
    print(f"\n  {YELLOW}الحل / Fix:{RESET}")
    for line in fix.strip().split("\n"):
        print(f"    {line}")
    print()
    sys.exit(1)


def warn(msg):
    print(f"  {YELLOW}[WARN]{RESET} {msg}")


print("\n" + "=" * 68)
print("  EcoMine Observatory — فحص البيئة / Environment check")
print("=" * 68)


# ---------------------------------------------------------------- 1. Python
print("\n[1/7] إصدار Python / Python version")
v = sys.version_info
if v < (3, 10):
    fail(
        f"Python {v.major}.{v.minor} — المشروع يتطلب 3.10 أو أحدث",
        "ثبّت Python 3.10+ من python.org ثم أنشئ بيئة افتراضية جديدة:\n"
        "  python3.10 -m venv .venv\n"
        "  source .venv/bin/activate   # Windows: .venv\\Scripts\\activate",
    )
ok(f"Python {v.major}.{v.minor}.{v.micro}")


# ---------------------------------------------------- 2. البيئة الافتراضية
print("\n[2/7] البيئة الافتراضية / Virtual environment")
in_venv = hasattr(sys, "real_prefix") or sys.base_prefix != sys.prefix
if in_venv:
    ok(f"نشطة / active: {sys.prefix}")
else:
    warn("لا تعمل داخل بيئة افتراضية — ليس خطأً، لكنه يخلط التبعيات مع النظام")


# ------------------------------------------------------------- 3. الحزم
print("\n[3/7] الحزم المطلوبة / Required packages")
required = {
    "ee": "earthengine-api",
    "geemap": "geemap",
    "leafmap": "leafmap",
}
missing = []
for mod, pkg in required.items():
    try:
        m = __import__(mod)
        ver = getattr(m, "__version__", "?")
        ok(f"{pkg} ({ver})")
    except ImportError:
        print(f"  {RED}[FAIL]{RESET} {pkg} غير مثبّت / not installed")
        missing.append(pkg)

if missing:
    fail(
        f"حزم ناقصة / missing: {', '.join(missing)}",
        "pip install " + " ".join(missing) + "\n"
        "أو دفعة واحدة / or all at once:\n"
        "  pip install -r requirements.txt",
    )


# --------------------------------------------------- 4. إعداد المشروع
print("\n[4/7] معرّف المشروع / Project ID")
if not EE_PROJECT or EE_PROJECT == "your-gee-project-id-here":
    fail(
        "لم يُضبط معرّف المشروع / project ID not set",
        "cp .env.example .env\n"
        "ثم حرّر .env وضع معرّف مشروعك:\n"
        "  EE_PROJECT=my-project-id\n\n"
        "ملف .env مستثنى من git ولن يُرفع.",
    )
ok(f"EE_PROJECT = {EE_PROJECT}  (من .env / from .env)")


# ------------------------------------------------------ 5. المصادقة
print("\n[5/7] المصادقة / Authentication")
import ee  # noqa: E402

try:
    ee.Initialize(project=EE_PROJECT)
    ok("ee.Initialize نجح / succeeded")
except Exception as e:
    msg = str(e)
    if "not registered" in msg or "not signed up" in msg or "permission" in msg.lower():
        fail(
            "المشروع غير مسجّل في Earth Engine / project not registered",
            "1. افتح https://code.earthengine.google.com/register\n"
            "2. سجّل مشروع Google Cloud الخاص بك (نوع non-commercial مجاني للبحث)\n"
            "3. الموافقة قد تستغرق من دقائق إلى أيام\n"
            f"4. تأكّد أن EE_PROJECT في هذا الملف = معرّف المشروع المسجّل\n\n"
            f"الرسالة الأصلية / original error:\n{msg[:300]}",
        )
    elif "credentials" in msg.lower() or "authenticate" in msg.lower():
        fail(
            "لا توجد صلاحيات محفوظة / no stored credentials",
            "شغّل في الطرفية / run in your terminal:\n"
            "  earthengine authenticate\n\n"
            "سيفتح المتصفح — سجّل الدخول والصق الرمز.\n"
            "ثم أعد تشغيل هذا الملف.",
        )
    else:
        fail(
            "فشل التهيئة / initialization failed",
            f"الرسالة / error:\n{msg[:400]}\n\n"
            "جرّب: earthengine authenticate --force",
        )


# --------------------------------------------- 6. اتصال فعلي بالخادم
print("\n[6/7] اتصال فعلي بالخادم / Live server round-trip")
try:
    result = ee.Number(6).multiply(7).getInfo()
    if result == 42:
        ok("الخادم يستجيب ويحسب / server responds and computes")
    else:
        fail(f"استجابة غير متوقعة: {result}", "أعد المصادقة: earthengine authenticate --force")
except Exception as e:
    fail(
        "لا يمكن الوصول إلى خوادم Earth Engine / cannot reach servers",
        f"تحقّق من الاتصال بالإنترنت والجدار الناري.\n\nالرسالة / error:\n{str(e)[:300]}",
    )


# ------------------------------------- 7. الوصول إلى مجموعات البيانات
print("\n[7/7] الوصول إلى بيانات Sentinel / Sentinel data access")

# نقطة اختبار: الدرع العربي قرب الدويحي / test point near Ad Duwayhi
test_point = ee.Geometry.Point([41.55, 22.44]).buffer(3000)

try:
    s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
          .filterBounds(test_point)
          .filterDate("2025-01-01", "2025-12-31"))
    n2 = s2.size().getInfo()
    if n2 > 0:
        ok(f"Sentinel-2: {n2} مشهد متاح / scenes available")
    else:
        warn("Sentinel-2: صفر مشاهد — تحقّق من الإحداثيات والتواريخ")
except Exception as e:
    fail("تعذّر الوصول إلى Sentinel-2", f"{str(e)[:300]}")

try:
    s1 = (ee.ImageCollection("COPERNICUS/S1_GRD")
          .filterBounds(test_point)
          .filterDate("2025-01-01", "2025-12-31")
          .filter(ee.Filter.eq("instrumentMode", "IW")))
    n1 = s1.size().getInfo()
    if n1 > 0:
        ok(f"Sentinel-1: {n1} مشهد متاح / scenes available")
    else:
        warn("Sentinel-1: صفر مشاهد في هذا النطاق")
except Exception as e:
    fail("تعذّر الوصول إلى Sentinel-1", f"{str(e)[:300]}")


# ------------------------------- فحص إضافي: أسماء مفاتيح المئينات
# هذا هو الاستدعاء الذي أشك أنه قد يفشل في المرحلة ٢، فنختبره الآن مبكرًا
print("\n[إضافي] تسمية مفاتيح reduceRegion / percentile key naming")
try:
    img = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
           .filterBounds(test_point)
           .filterDate("2025-01-01", "2025-06-30")
           .first())
    ndvi = img.normalizedDifference(["B8", "B4"]).rename("dNDVI")
    stats = ndvi.reduceRegion(
        reducer=ee.Reducer.percentile([16, 50, 84]),
        geometry=test_point, scale=100, maxPixels=1e8, bestEffort=True,
    ).getInfo()
    keys = sorted(stats.keys())
    ok(f"المفاتيح الراجعة / returned keys: {keys}")
    expected = {"dNDVI_p16", "dNDVI_p50", "dNDVI_p84"}
    if expected.issubset(set(keys)):
        ok("التسمية تطابق ما يفترضه ecomine_stage2.py")
    else:
        warn(
            f"التسمية مختلفة عمّا يفترضه الكود!\n"
            f"         المتوقّع / expected: {sorted(expected)}\n"
            f"         الفعلي  / actual:   {keys}\n"
            f"         عدّل أسماء المئينات في robust_null() داخل "
            f"ecomine_stage2.py لتطابق الفعلي أعلاه.\n"
            f"         Adjust the percentile key names in robust_null() in "
            f"ecomine_stage2.py to match the actual keys above."
        )
except Exception as e:
    warn(f"تعذّر اختبار المئينات: {str(e)[:200]}")


# ------------------------------------------------------------ الخلاصة
print("\n" + "=" * 68)
print(f"  {GREEN}البيئة جاهزة / Environment ready{RESET}")
print("=" * 68)
print("""
الخطوة التالية / Next step:

  jupyter lab notebooks/ecomine_stage1.ipynb

شغّل كل الخلايا، ثم تحقّق بصريًا:
هل ترى المنجم؟ وفي أي طبقات (BSI؟ S1 VV؟ كلاهما؟)
وهل هو في وسط الإطار أم خارجه؟

إن كان خارج الإطار، فإحداثيات الموقع خاطئة. حرّك الخريطة حتى تجد المنجم،
اقرأ الإحداثيات الحقيقية، وحدّث مدخل الموقع في ecomine_step1.py قبل حساب
أي مؤشّر. كل رقم لاحق يُحسب فوق هذا الصندوق، فصندوق خاطئ يعني أرقامًا
تصف الأرض الخطأ.

Run all cells, then check visually: is the mine visible, in which layers, and
is it centred in the frame? If it is outside the frame the site coordinates are
wrong. Pan until you find the pit, read the true coordinates, and update the
site entry in ecomine_step1.py BEFORE computing any indicator. Every later
number is computed over this box.
""")
