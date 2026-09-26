# -*- coding: utf-8 -*-
"""
مُجمّع القصص المصورة إلى فيديو Shorts عمودي (9:16)
====================================================
يستقبل ملف ZIP الناتج من أداة توليد القصص (story.json + images/ + audio/)
ويجمّعه في فيديو واحد بصيغة 1080x1920، مع تأثير تكبير بطيء اختياري وترجمة
نصية محروقة اختيارية، ثم يقسّمه إلى مقاطع Shorts حسب المدة المطلوبة.
"""

import streamlit as st
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

st.set_page_config(
    page_title="مُجمّع القصص إلى Shorts",
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

st.title("🎬 مُجمّع القصص إلى فيديو Shorts")
st.caption(
    "ارفع ملف الـ ZIP الناتج من أداة توليد القصة (نص + صور + صوت)، "
    "وسيتم تجميعه في فيديو عمودي (9:16) جاهز للنشر، مع إمكانية تقسيمه إلى مقاطع قصيرة."
)

with st.sidebar:
    st.header("⚙️ الإعدادات")
    add_subs = st.checkbox("حرق الترجمة النصية على الفيديو", value=True)
    zoom_effect = st.checkbox("تأثير تكبير بطيء (Ken Burns)", value=True)
    max_clip_seconds = st.slider(
        "أقصى مدة لكل مقطع (ثانية) — 0 يعني بدون تقسيم",
        0, 180, 0, step=10,
    )
    fps = 25

uploaded_zip = st.file_uploader("📦 ارفع ملف ZIP القصة", type=["zip"])
generate_clicked = st.button("🚀 إنشاء الفيديو", use_container_width=True, type="primary")


# =========================================================
# دوال مساعدة
# =========================================================

def run_cmd(cmd: list, desc: str = ""):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"فشل تنفيذ: {desc}\n{result.stderr[-2500:]}")
    return result


def get_duration(media_path: Path) -> float:
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
    chunks = split_narration(narration)
    total_chars = sum(len(c) for c in chunks) or 1
    lines = []
    t = 0.0
    for i, chunk in enumerate(chunks, start=1):
        share = len(chunk) / total_chars
        seg_dur = max(1.2, duration * share)
        start, end = t, min(t + seg_dur, duration)
        lines.append(str(i))
        lines.append(f"{format_srt_time(start)} --> {format_srt_time(end)}")
        lines.append(chunk)
        lines.append("")
        t = end
    out_path.write_text("\n".join(lines), encoding="utf-8")


def build_scene_clip(image_path: Path, audio_path: Path, duration: float,
                      srt_path: Path, out_path: Path, zoom: bool, subs: bool):
    frames = max(1, int(round(duration * fps)))

    filters = ["scale=1080:1920:force_original_aspect_ratio=increase", "crop=1080:1920"]

    if zoom:
        filters.append(
            f"zoompan=z='min(zoom+0.0012,1.15)':d={frames}:s=1080x1920:fps={fps}"
        )
    else:
        filters.append(f"fps={fps}")

    if subs and srt_path.exists() and srt_path.stat().st_size > 0:
        srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
        style = (
            "FontName=Noto Sans Arabic,FontSize=18,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BorderStyle=3,Outline=2,Shadow=0,"
            "Alignment=2,MarginV=90"
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
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            str(out_path),
        ],
        f"بناء مشهد {image_path.name}",
    )


def concat_clips(clip_paths: list, out_path: Path, workdir: Path):
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
    """يقسّم قائمة (فهرس، مدة) إلى مجموعات لا تتجاوز كل منها max_seconds."""
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

        # 2) بناء كل مشهد كفيديو منفصل
        clip_paths, scene_durations = [], []
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
            audio_path = audio_candidates[0]

            duration = get_duration(audio_path)
            scene_durations.append(duration)

            srt_path = workdir / f"sub_{i}.srt"
            if add_subs:
                build_scene_srt(scene.get("narration", ""), duration, srt_path)

            clip_path = workdir / f"scene_{i}.mp4"
            build_scene_clip(image_path, audio_path, duration, srt_path,
                              clip_path, zoom_effect, add_subs)
            clip_paths.append(clip_path)

        # 3) تجميع المشاهد ضمن مقاطع نهائية حسب الحد الأقصى للمدة
        status.update(label="🎬 جاري دمج المشاهد في الفيديو النهائي...")
        groups = group_scenes_by_duration(scene_durations, max_clip_seconds)

        results = []
        for gi, group in enumerate(groups):
            group_clips = [clip_paths[i] for i in group]
            group_duration = sum(scene_durations[i] for i in group)
            out_name = f"{sanitize_filename(title)}_part{gi + 1}.mp4" if len(groups) > 1 \
                else f"{sanitize_filename(title)}.mp4"
            out_path = workdir / out_name
            concat_clips(group_clips, out_path, workdir)
            results.append({"path": out_path, "duration": group_duration, "index": gi + 1})

        status.update(label="✅ اكتمل الإنشاء بنجاح!", state="complete")

        st.success(f"تم إنشاء {len(results)} فيديو بنجاح 🎉")
        for r in results:
            st.subheader(
                f"الجزء {r['index']}" if len(results) > 1 else title
            )
            st.caption(f"⏱️ المدة: {r['duration']:.0f} ثانية")
            st.video(str(r["path"]))
            with open(r["path"], "rb") as f:
                st.download_button(
                    label="⬇️ تنزيل الفيديو",
                    data=f.read(),
                    file_name=r["path"].name,
                    mime="video/mp4",
                    use_container_width=True,
                    key=f"dl_{r['index']}",
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
- كل مشهد يُعرض طوال مدة صوته بالضبط، مع تأثير تكبير بطيء اختياري وترجمة نصية اختيارية.
- إذا حددت مدة قصوى للمقطع أكبر من صفر، سيتم تقسيم القصة إلى عدة فيديوهات، كل واحد لا يتجاوز
  تلك المدة (بحدود المشاهد الكاملة، دون قطع مشهد في المنتصف).
        """
    )
