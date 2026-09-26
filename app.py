# -*- coding: utf-8 -*-
"""
مُجمّع القصص المصورة إلى فيديو (16:9 و/أو Shorts عمودي 9:16)
====================================================
يستقبل ملف ZIP الناتج من أداة توليد القصص (story.json + images/ + audio/)
ويجمّعه في فيديو نهائي، مع دعم مخرجين:
  - الفيديو الرئيسي بعرض 16:9 (لليوتيوب).
  - مقاطع Shorts/Reels عمودية 9:16 مع قصّ تلقائي "ذكي" حول الوجه إن أمكن.

يضمن الكود تطابق مدة كل مشهد (صورة + ترجمة) تماماً مع مدة الصوت الخاصة به
(audio duration عبر ffprobe)، ولا يضيف أي موسيقى خلفية إطلاقاً، مع دعم
اختياري لمؤثرات صوتية طبيعية إن توفرت ملفاتها محلياً في مجلد sfx_library/.
"""

import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False


st.set_page_config(
    page_title="مُجمّع القصص إلى فيديو",
    page_icon="🎬",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    .stButton>button { height: 3em; font-size: 1.1em; border-radius: 10px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🎬 مُجمّع القصص إلى فيديو")
st.caption(
    "ارفع ملف الـ ZIP الناتج من أداة توليد القصة (نص + صور + صوت)، "
    "واختر الفيديو الرئيسي (16:9) و/أو مقاطع Shorts عمودية (9:16) بقصّ "
    "تلقائي، بدون أي موسيقى خلفية."
)

# --------------------------------------------------------------------------
# إعدادات ثابتة
# --------------------------------------------------------------------------
FPS = 25
LANDSCAPE_SIZE = (1920, 1080)   # 16:9
PORTRAIT_SIZE = (1080, 1920)   # 9:16

SFX_LIBRARY_DIR = Path(__file__).resolve().parent / "sfx_library"
SFX_FILENAMES = {
    "birds": "birds.mp3",
    "rain": "rain.mp3",
    "door_open": "door_open.mp3",
    "children_laughing": "children_laughing.mp3",
    "footsteps": "footsteps.mp3",
    "wind": "wind.mp3",
}

MODE_LANDSCAPE = "landscape"
MODE_PORTRAIT = "portrait"

OUTPUT_CHOICES = {
    "الفيديو الرئيسي فقط (16:9)": (MODE_LANDSCAPE,),
    "مقاطع Shorts فقط (9:16)": (MODE_PORTRAIT,),
    "كلاهما معاً": (MODE_LANDSCAPE, MODE_PORTRAIT),
}

MODE_LABELS = {
    MODE_LANDSCAPE: "الفيديو الرئيسي (16:9)",
    MODE_PORTRAIT: "Shorts عمودي (9:16)",
}

with st.sidebar:
    st.header("⚙️ الإعدادات")
    output_choice_label = st.radio("نوع المخرج", list(OUTPUT_CHOICES.keys()), index=2)
    add_subs = st.checkbox("حرق الترجمة النصية على الفيديو", value=True)
    zoom_effect = st.checkbox("تأثير تكبير بطيء (Ken Burns)", value=True)
    add_sfx = st.checkbox(
        "إضافة مؤثرات صوتية طبيعية إن توفرت ملفاتها (بدون أي موسيقى إطلاقاً)",
        value=True,
    )
    smart_crop = st.checkbox(
        "قصّ ذكي حول الوجه لمقاطع Shorts (يتطلب opencv)",
        value=True,
        disabled=not _CV2_AVAILABLE,
        help=None if _CV2_AVAILABLE else "مكتبة opencv-python-headless غير مثبتة على الخادم.",
    )
    max_clip_seconds = st.slider(
        "أقصى مدة لكل مقطع (ثانية) — 0 يعني بدون تقسيم",
        0, 180, 0, step=10,
    )

output_modes = OUTPUT_CHOICES[output_choice_label]

uploaded_zip = st.file_uploader("📦 ارفع ملف ZIP القصة", type=["zip"])
generate_clicked = st.button("🚀 إنشاء الفيديو", use_container_width=True, type="primary")


# =========================================================
# دوال مساعدة عامة
# =========================================================

def run_cmd(cmd: list, desc: str = ""):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"فشل تنفيذ: {desc}\n{result.stderr[-2500:]}")
    return result


def get_duration(media_path: Path) -> float:
    """مدة ملف الصوت/الفيديو بدقة عبر ffprobe — هي المرجع الوحيد لطول كل مشهد."""
    result = run_cmd(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(media_path),
        ],
        "قراءة مدة الملف",
    )
    return float(result.stdout.strip())


def format_srt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3600 * 1000)
    minutes, rem = divmod(rem, 60 * 1000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def split_narration(text: str):
    """يقسّم نص السرد إلى جمل قصيرة قابلة للعرض كترجمة."""
    parts = re.split(r"(?<=[.!؟،])\s+", text.strip())
    parts = [p.strip() for p in parts if p.strip()]
    return parts if parts else [text.strip()]


def build_scene_srt(narration: str, duration: float, out_path: Path):
    """
    يبني ملف SRT لمشهد واحد، بتوزيع المدة على الجمل حسب عدد الكلمات
    (تقدير أدق من التوزيع الحرفي لأنه أقرب لمعدل النطق الفعلي).
    """
    chunks = split_narration(narration)
    word_counts = [max(1, len(c.split())) for c in chunks]
    total_words = sum(word_counts) or 1

    lines = []
    t = 0.0
    for i, (chunk, wc) in enumerate(zip(chunks, word_counts), start=1):
        share = wc / total_words
        seg_dur = max(1.0, duration * share)
        start, end = t, min(t + seg_dur, duration)
        lines.append(str(i))
        lines.append(f"{format_srt_time(start)} --> {format_srt_time(end)}")
        lines.append(chunk)
        lines.append("")
        t = end
    out_path.write_text("\n".join(lines), encoding="utf-8")


def get_image_size(image_path: Path):
    if not _PIL_AVAILABLE:
        return None
    try:
        with Image.open(image_path) as img:
            return img.size  # (width, height)
    except Exception:
        return None


# =========================================================
# القصّ الذكي حول الوجه (لمقاطع 9:16 فقط)
# =========================================================
_face_cascade = None


def _get_face_cascade():
    global _face_cascade
    if _face_cascade is None and _CV2_AVAILABLE:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _face_cascade = cv2.CascadeClassifier(cascade_path)
    return _face_cascade


def detect_face_center_ratio(image_path: Path):
    """
    يحاول كشف أكبر وجه في الصورة، ويعيد نسبة موقعه الأفقي (0.0 إلى 1.0)
    بالنسبة لعرض الصورة الأصلية، أو None إن تعذّر الكشف.
    """
    if not _CV2_AVAILABLE:
        return None
    try:
        img = cv2.imread(str(image_path))
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        cascade = _get_face_cascade()
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
        if len(faces) == 0:
            return None
        largest = max(faces, key=lambda f: f[2] * f[3])
        x, y, w, h = largest
        face_center_x = x + w / 2.0
        img_w = img.shape[1]
        if img_w <= 0:
            return None
        return face_center_x / img_w
    except Exception:
        return None


def compute_portrait_crop_x(orig_w: int, orig_h: int, face_center_ratio) -> float:
    """
    يحسب إحداثي x (بمقياس الصورة بعد تكبيرها لتغطية 1080x1920) الذي يجعل
    نافذة القصّ 1080x1920 متمركزة حول الوجه المكتشف، مع البقاء داخل حدود
    الصورة المُكبَّرة. إن لم يوجد وجه، يُستخدم المنتصف الأفقي كإجراء احتياطي.
    """
    target_w, target_h = PORTRAIT_SIZE
    scale = max(target_w / orig_w, target_h / orig_h)
    scaled_w = orig_w * scale

    ratio = 0.5 if face_center_ratio is None else face_center_ratio
    face_center_scaled_x = ratio * scaled_w

    x = face_center_scaled_x - target_w / 2.0
    x = max(0.0, min(x, max(0.0, scaled_w - target_w)))
    return x


# =========================================================
# مزج المؤثرات الصوتية الاختيارية (بدون أي موسيقى)
# =========================================================
def prepare_scene_audio(narration_path: Path, sfx_keyword: str, add_sfx: bool,
                         workdir: Path, idx: int) -> Path:
    """
    إن كان هناك مؤثر صوتي مناسب مذكور في story.json (sfx) وتوفّر ملفه
    فعلياً داخل sfx_library/، يُمزج بمستوى صوت منخفض تحت التعليق الصوتي
    ضمن نفس مدة السرد بالضبط (لا يُطوَّل المشهد بسبب المؤثر). إن لم يتوفر
    الملف، يُتجاهل الأمر بصمت ويُستخدم صوت السرد كما هو (بدون أي موسيقى).
    """
    if not add_sfx or not sfx_keyword or sfx_keyword == "none":
        return narration_path

    sfx_filename = SFX_FILENAMES.get(sfx_keyword)
    if not sfx_filename:
        return narration_path

    sfx_path = SFX_LIBRARY_DIR / sfx_filename
    if not sfx_path.exists():
        return narration_path

    duration = get_duration(narration_path)
    mixed_path = workdir / f"scene_{idx}_mixed.wav"

    run_cmd(
        [
            "ffmpeg", "-y",
            "-i", str(narration_path),
            "-stream_loop", "-1", "-i", str(sfx_path),
            "-filter_complex",
            f"[1:a]volume=0.18,atrim=0:{duration}[sfx];"
            f"[0:a][sfx]amix=inputs=2:duration=first:dropout_transition=0[aout]",
            "-map", "[aout]",
            "-t", str(duration),
            "-c:a", "pcm_s16le",
            str(mixed_path),
        ],
        f"مزج المؤثر الصوتي للمشهد {idx}",
    )
    return mixed_path


# =========================================================
# بناء مقطع مشهد واحد (فيديو) لأي من الوضعين
# =========================================================
def build_scene_clip(image_path: Path, audio_path: Path, duration: float,
                      srt_path: Path, out_path: Path, mode: str,
                      zoom: bool, subs: bool, crop_x=None):
    target_w, target_h = LANDSCAPE_SIZE if mode == MODE_LANDSCAPE else PORTRAIT_SIZE
    frames = max(1, int(round(duration * FPS)))

    filters = [f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase"]

    if mode == MODE_PORTRAIT and crop_x is not None:
        filters.append(f"crop={target_w}:{target_h}:{crop_x:.0f}:(ih-{target_h})/2")
    else:
        filters.append(f"crop={target_w}:{target_h}")

    if zoom:
        filters.append(
            f"zoompan=z='min(zoom+0.0012,1.15)':d={frames}:s={target_w}x{target_h}:fps={FPS}"
        )
    else:
        filters.append(f"fps={FPS}")

    if subs and srt_path.exists() and srt_path.stat().st_size > 0:
        srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
        # في الوضع العمودي تُرفع الترجمة قليلاً عن الأسفل لتفادي حجب الوجوه
        # ولتبقى ضمن المنطقة الآمنة بعيداً عن أزرار واجهة تطبيقات Shorts.
        margin_v = 60 if mode == MODE_LANDSCAPE else 110
        font_size = 26 if mode == MODE_LANDSCAPE else 20
        style = (
            f"FontName=Noto Sans Arabic,FontSize={font_size},PrimaryColour=&H00FFFFFF,"
            f"OutlineColour=&H00000000,BorderStyle=3,Outline=2,Shadow=0,"
            f"Alignment=2,MarginV={margin_v}"
        )
        filters.append(f"subtitles='{srt_escaped}':force_style='{style}'")

    vf = ",".join(filters)

    run_cmd(
        [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(image_path),
            "-i", str(audio_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            str(out_path),
        ],
        f"بناء مشهد {image_path.name} ({MODE_LABELS[mode]})",
    )


def concat_clips(clip_paths: list, out_path: Path):
    """يدمج المقاطع بإعادة الترميز (أكثر أماناً وتوافقاً من النسخ المباشر)."""
    if len(clip_paths) == 1:
        shutil.copy(clip_paths[0], out_path)
        return

    inputs = []
    for p in clip_paths:
        inputs += ["-i", str(p)]

    n = len(clip_paths)
    filter_parts = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    filter_complex = f"{filter_parts}concat=n={n}:v=1:a=1[outv][outa]"

    run_cmd(
        [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            str(out_path),
        ],
        "دمج المقاطع",
    )


def group_scenes_by_duration(scene_durations: list, max_seconds: int):
    """يقسّم قائمة المدد إلى مجموعات لا تتجاوز كل منها max_seconds."""
    if max_seconds <= 0:
        return [list(range(len(scene_durations)))]

    groups, current, current_total = [], [], 0.0
    for i, d in enumerate(scene_durations):
        if current and current_total + d > max_seconds:
            groups.append(current)
            current, current_total = [], 0.0
        current.append(i)
        current_total += d
    if current:
        groups.append(current)
    return groups


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\s\u0600-\u06FF-]", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name[:60] or "video"


# =========================================================
# المنطق الرئيسي
# =========================================================
if generate_clicked:
    if not uploaded_zip:
        st.error("الرجاء رفع ملف ZIP أولاً.")
        st.stop()
    if not shutil.which("ffmpeg"):
        st.error(
            "لم يتم العثور على ffmpeg على الخادم. تأكد من وجود ملف packages.txt "
            "يحتوي على 'ffmpeg' و'fonts-noto' في جذر المستودع."
        )
        st.stop()

    workdir = Path(tempfile.mkdtemp(prefix="assembler_"))
    status = st.status("جاري المعالجة...", expanded=True)

    try:
        # 1) فك ضغط الملف
        status.update(label="📦 جاري فك ضغط الملف...")
        zip_path = workdir / "story.zip"
        zip_path.write_bytes(uploaded_zip.read())
        extract_dir = workdir / "extracted"
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        story_json_path = next(extract_dir.rglob("story.json"), None)
        if not story_json_path:
            raise FileNotFoundError("لم يتم العثور على story.json داخل الملف المضغوط.")

        story = json.loads(story_json_path.read_text(encoding="utf-8"))
        base_dir = story_json_path.parent
        scenes = story.get("scenes", [])
        if not scenes:
            raise ValueError("لم يتم العثور على أي مشاهد (scenes) داخل story.json.")

        title = story.get("title", "قصة")

        if MODE_PORTRAIT in output_modes and smart_crop and not _CV2_AVAILABLE:
            st.warning(
                "⚠️ مكتبة opencv-python-headless غير متوفرة، سيُستخدم قصّ "
                "مركزي بسيط بدل القصّ الذكي حول الوجه."
            )

        # 2) تجهيز بيانات كل مشهد (صوت + ترجمة + قصّ ذكي) مرة واحدة فقط،
        #    ثم إعادة استخدامها في أي عدد من أوضاع الإخراج.
        scene_data = []
        for i, scene in enumerate(scenes):
            n = scene.get("scene_number", i + 1)
            status.update(label=f"✂️ جاري تجهيز المشهد {i + 1}/{len(scenes)}...")

            image_candidates = [p for p in base_dir.rglob(f"scene_{n}.*")
                                 if p.suffix.lower() in (".png", ".jpg", ".jpeg")]
            audio_candidates = [p for p in base_dir.rglob(f"scene_{n}.*")
                                 if p.suffix.lower() in (".wav", ".mp3", ".m4a")]

            if not image_candidates or not audio_candidates:
                raise FileNotFoundError(f"ملفات ناقصة للمشهد رقم {n}.")

            image_path = image_candidates[0]
            narration_audio_path = audio_candidates[0]

            # مدة المشهد = مدة صوت السرد الأصلي بالضبط (مرجع المزامنة الوحيد)
            duration = get_duration(narration_audio_path)

            sfx_keyword = scene.get("sfx", "none")
            scene_audio_path = prepare_scene_audio(
                narration_audio_path, sfx_keyword, add_sfx, workdir, i
            )

            srt_path = workdir / f"sub_{i}.srt"
            if add_subs:
                build_scene_srt(scene.get("narration", ""), duration, srt_path)

            crop_x = None
            if MODE_PORTRAIT in output_modes:
                size = get_image_size(image_path)
                if size:
                    orig_w, orig_h = size
                    face_ratio = detect_face_center_ratio(image_path) if smart_crop else None
                    crop_x = compute_portrait_crop_x(orig_w, orig_h, face_ratio)

            scene_data.append({
                "index": i,
                "image_path": image_path,
                "audio_path": scene_audio_path,
                "duration": duration,
                "srt_path": srt_path,
                "crop_x": crop_x,
            })

        scene_durations = [s["duration"] for s in scene_data]
        groups = group_scenes_by_duration(scene_durations, max_clip_seconds)

        # 3) بناء المخرجات لكل وضع مطلوب (16:9 و/أو 9:16)
        all_results = {}
        for mode in output_modes:
            mode_label = MODE_LABELS[mode]
            clip_paths = []
            for s in scene_data:
                status.update(
                    label=f"🎬 جاري بناء مشهد {s['index'] + 1}/{len(scene_data)} — {mode_label}..."
                )
                clip_path = workdir / f"scene_{s['index']}_{mode}.mp4"
                build_scene_clip(
                    s["image_path"], s["audio_path"], s["duration"], s["srt_path"],
                    clip_path, mode, zoom_effect, add_subs, crop_x=s["crop_x"],
                )
                clip_paths.append(clip_path)

            status.update(label=f"🧩 جاري دمج مشاهد {mode_label}...")
            mode_results = []
            for gi, group in enumerate(groups):
                group_clips = [clip_paths[i] for i in group]
                group_duration = sum(scene_durations[i] for i in group)
                suffix = "_shorts" if mode == MODE_PORTRAIT else ""
                out_name = (
                    f"{sanitize_filename(title)}{suffix}_part{gi + 1}.mp4"
                    if len(groups) > 1 else f"{sanitize_filename(title)}{suffix}.mp4"
                )
                out_path = workdir / out_name
                concat_clips(group_clips, out_path)
                mode_results.append({"path": out_path, "duration": group_duration, "index": gi + 1})

            all_results[mode] = mode_results

        status.update(label="✅ اكتمل الإنشاء بنجاح!", state="complete")

        # 4) عرض النتائج (تبويب لكل وضع إن كان الاثنان مطلوبين معاً)
        if len(output_modes) > 1:
            tabs = st.tabs([MODE_LABELS[m] for m in output_modes])
        else:
            tabs = [st.container()]

        for mode, tab in zip(output_modes, tabs):
            with tab:
                results = all_results[mode]
                st.success(f"تم إنشاء {len(results)} فيديو ({MODE_LABELS[mode]}) 🎉")
                for r in results:
                    st.subheader(f"الجزء {r['index']}" if len(results) > 1 else title)
                    st.caption(f"⏱️ المدة: {r['duration']:.0f} ثانية")
                    st.video(str(r["path"]))
                    with open(r["path"], "rb") as f:
                        st.download_button(
                            label="⬇️ تنزيل الفيديو",
                            data=f.read(),
                            file_name=r["path"].name,
                            mime="video/mp4",
                            use_container_width=True,
                            key=f"dl_{mode}_{r['index']}",
                        )
                    st.divider()

    except Exception as e:
        status.update(label="❌ حدث خطأ", state="error")
        st.error(f"حدث خطأ أثناء المعالجة:\n\n{e}")


with st.expander("ℹ️ ملاحظات مهمة"):
    st.markdown(
        """
- الملف المضغوط يجب أن يحتوي على `story.json` ومجلدي `images/` و`audio/` بنفس أسماء المشاهد
  (`scene_1.png`, `scene_1.wav`, ...) كما تنتجه أداة توليد القصص.
- عند النشر على **Streamlit Community Cloud** تأكد من وجود ملف `packages.txt` يحتوي على
  السطرين `ffmpeg` و`fonts-noto` لضمان عمل حرق الترجمة العربية بشكل صحيح.
- كل مشهد يُعرض طوال مدة صوت السرد الخاص به بالضبط (عبر ffprobe)، مع تأثير
  تكبير بطيء اختياري وترجمة نصية اختيارية.
- **لا تُضاف أي موسيقى خلفية إطلاقاً.** المؤثرات الصوتية الطبيعية (عصافير،
  مطر، ضحكات أطفال...) اختيارية تماماً، ولا تعمل إلا إذا وضعت الملفات
  الفعلية بنفسك داخل مجلد `sfx_library/` بجانب app.py بهذه الأسماء بالضبط:
  `birds.mp3`, `rain.mp3`, `door_open.mp3`, `children_laughing.mp3`,
  `footsteps.mp3`, `wind.mp3`. أي مؤثر غير موجود يُتجاهل بصمت دون أي خطأ.
- مقاطع **Shorts (9:16)** تُقصّ تلقائياً حول أكبر وجه مكتشف في الصورة
  (عبر OpenCV) إن وُجد، وإلا فيُستخدم القصّ المركزي كإجراء احتياطي.
- إذا حددت مدة قصوى للمقطع أكبر من صفر، سيتم تقسيم كل وضع إخراج إلى عدة
  فيديوهات، كل واحد لا يتجاوز تلك المدة (بحدود المشاهد الكاملة).
        """
    )
