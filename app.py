# -*- coding: utf-8 -*-
"""
أداة تقطيع وتحويل الفيديو الرئيسي إلى مقاطع Shorts عمودية (9:16)
================================================================
تستقبل ملف فيديو رئيسي واحد (MP4) وتُخرج عدة مقاطع Shorts جاهزة:
  - تحويل الأبعاد إلى عمودي (9:16) عبر خلفية مموّهة (Blurred Background
    Padding) حول الفيديو الأصلي كاملاً، بدون أي اقتصاص أو تشويه للمحتوى.
  - تقطيع الفيديو الطويل تلقائياً إلى أجزاء متتالية (Part 1, Part 2, ...)
    بطول محدد (افتراضياً حوالي 55 ثانية)، عبر مُقسِّم FFmpeg المدمج
    (segment)، بنسخ مباشر للترميز (-c copy) لأعلى سرعة.

لا يوجد أي استخدام لـ MoviePy في هذا الملف إطلاقاً — كل العمليات أوامر
FFmpeg مباشرة عبر subprocess، لتفادي أي بطء أو Throttling على خادم
Streamlit Community Cloud المحدود الموارد.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="مُقطّع Shorts",
    page_icon="✂️",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.title("✂️ مُقطّع Shorts (تحويل عمودي + تقطيع تلقائي)")
st.caption(
    "ارفع الفيديو الرئيسي الجاهز (16:9)، وستحصل على عدة مقاطع عمودية "
    "(9:16) جاهزة للنشر كـ Shorts/Reels، بخلفية مموّهة بدل الاقتصاص، "
    "مقسّمة تلقائياً إلى أجزاء متتالية."
)

# --------------------------------------------------------------------------
# إعدادات ثابتة
# --------------------------------------------------------------------------
PORTRAIT_W, PORTRAIT_H = 1080, 1920

PRESETS = {
    "سريع (أقل جودة قليلاً)": ("veryfast", 26),
    "متوازن": ("fast", 23),
    "أعلى جودة (أبطأ)": ("medium", 20),
}

with st.sidebar:
    st.header("⚙️ الإعدادات")
    segment_seconds = st.slider("مدة كل جزء (ثانية)", 20, 90, 55, step=5)
    blur_strength = st.slider("قوة تمويه الخلفية", 5, 40, 20, step=5)
    output_quality = st.selectbox(
        "جودة الإخراج", list(PRESETS.keys()), index=1
    )

uploaded_video = st.file_uploader("🎬 ارفع الفيديو الرئيسي", type=["mp4", "mov", "m4v"])
process_clicked = st.button("🚀 تحويل وتقطيع", use_container_width=True, type="primary")


# =========================================================
# دوال مساعدة
# =========================================================

def run_cmd(cmd: list, desc: str = ""):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"فشل تنفيذ: {desc}\n{result.stderr[-2500:]}")
    return result


def get_duration(path: Path) -> float:
    result = run_cmd(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        "قراءة مدة الفيديو",
    )
    return float(result.stdout.strip())


def get_fps(path: Path) -> float:
    result = run_cmd(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        "قراءة معدل الإطارات",
    )
    num, _, den = result.stdout.strip().partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 30.0


def has_audio_stream(path: Path) -> bool:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\s\u0600-\u06FF-]", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name[:60] or "video"


def build_vertical_blurred(input_path: Path, out_path: Path, preset: str, crf: int,
                            blur_sigma: int, fps: float, with_audio: bool):
    """
    يبني نسخة عمودية (1080x1920) من الفيديو كاملاً دون أي اقتصاص أو تشويه:
    - طبقة خلفية: نفس الفيديو مُكبَّر ليملأ الإطار بالكامل + تمويه قوي.
    - طبقة أمامية: نفس الفيديو بحجمه الطبيعي (محافظاً على نسبته الأصلية
      كاملة) وفي وسط الإطار، فيظهر المحتوى كاملاً بلا أي اقتصاص.
    فاصل الإطارات المرجعية (-g) يُضبط على ثانيتين لضمان تقطيع لاحق نظيف
    وسريع في خطوة split_into_parts.
    """
    gop = max(1, int(round(fps * 2)))
    filter_complex = (
        f"[0:v]scale={PORTRAIT_W}:{PORTRAIT_H}:force_original_aspect_ratio=increase,"
        f"crop={PORTRAIT_W}:{PORTRAIT_H},gblur=sigma={blur_sigma}[bg];"
        f"[0:v]scale={PORTRAIT_W}:{PORTRAIT_H}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto[outv]"
    )

    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]",
    ]
    if with_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    run_cmd(cmd, "تحويل الفيديو إلى عمودي بخلفية مموّهة")


def split_into_parts(input_path: Path, out_dir: Path, base_name: str, segment_seconds: int):
    """
    يقسّم الفيديو العمودي إلى أجزاء متتالية عبر مُقسِّم FFmpeg المدمج
    (segment) بنسخ الترميز مباشرة (-c copy) بدل إعادة الترميز، فيكون
    التقطيع شبه فوري بغض النظر عن طول الفيديو.
    """
    pattern = out_dir / f"{base_name}_part%03d.mp4"
    run_cmd(
        [
            "ffmpeg", "-y", "-i", str(input_path),
            "-c", "copy", "-map", "0",
            "-f", "segment", "-segment_time", str(segment_seconds),
            "-reset_timestamps", "1",
            str(pattern),
        ],
        "تقطيع الفيديو إلى أجزاء",
    )
    return sorted(out_dir.glob(f"{base_name}_part*.mp4"))


# =========================================================
# المنطق الرئيسي
# =========================================================
if process_clicked:
    if not uploaded_video:
        st.error("الرجاء رفع ملف الفيديو أولاً.")
        st.stop()
    if not shutil.which("ffmpeg"):
        st.error(
            "لم يتم العثور على ffmpeg على الخادم. تأكد من وجود ملف packages.txt "
            "يحتوي على 'ffmpeg' في جذر المستودع."
        )
        st.stop()

    workdir = Path(tempfile.mkdtemp(prefix="shorts_"))
    status = st.status("جاري المعالجة...", expanded=True)

    try:
        status.update(label="📥 جاري حفظ الملف المرفوع...")
        input_path = workdir / "input.mp4"
        input_path.write_bytes(uploaded_video.read())

        original_name = Path(uploaded_video.name).stem
        base_name = sanitize_filename(original_name)

        duration = get_duration(input_path)
        fps = get_fps(input_path)
        with_audio = has_audio_stream(input_path)
        preset, crf = PRESETS[output_quality]

        status.update(
            label=f"🎨 جاري التحويل إلى عمودي (9:16) بخلفية مموّهة... (المدة الكلية: {duration:.0f}ث)"
        )
        vertical_path = workdir / "vertical.mp4"
        build_vertical_blurred(input_path, vertical_path, preset, crf, blur_strength, fps, with_audio)

        status.update(label="✂️ جاري تقطيع الفيديو إلى أجزاء...")
        parts = split_into_parts(vertical_path, workdir, base_name, segment_seconds)

        if not parts:
            raise RuntimeError("لم ينتج أي جزء بعد التقطيع.")

        status.update(label=f"✅ اكتمل! تم إنتاج {len(parts)} جزء.", state="complete")

        st.success(f"تم إنشاء {len(parts)} مقطع Shorts جاهز 🎉")
        for i, part_path in enumerate(parts, start=1):
            part_duration = get_duration(part_path)
            st.subheader(f"الجزء {i}")
            st.caption(f"⏱️ المدة: {part_duration:.0f} ثانية")
            st.video(str(part_path))
            with open(part_path, "rb") as f:
                st.download_button(
                    label=f"⬇️ تنزيل الجزء {i}",
                    data=f.read(),
                    file_name=f"{base_name}_Part{i}.mp4",
                    mime="video/mp4",
                    use_container_width=True,
                    key=f"dl_part_{i}",
                )
            st.divider()

    except Exception as e:
        status.update(label="❌ حدث خطأ", state="error")
        st.error(f"حدث خطأ أثناء المعالجة:\n\n{e}")


with st.expander("ℹ️ ملاحظات مهمة"):
    st.markdown(
        """
- هذه الأداة تستقبل **فيديو رئيسي جاهز واحد (MP4)** وتُخرج منه عدة مقاطع
  Shorts عمودية — هي لا تولّد صوراً أو صوتاً، فقط تحوّل وتقطّع فيديو جاهز.
- **لا اقتصاص ولا تشويه للمحتوى الأصلي إطلاقاً**: يظهر الفيديو كاملاً
  بحجمه الطبيعي في المنتصف، وتُملأ الحواف العلوية/السفلية بخلفية مموّهة
  مأخوذة من نفس الفيديو (تقنية شائعة في أدوات تحويل المحتوى لـ Shorts).
- التقطيع يستخدم `-c copy` (نسخ مباشر بلا إعادة ترميز) فهو سريع جداً مهما
  طال الفيديو؛ إعادة الترميز الوحيدة تحدث مرة واحدة فقط أثناء خطوة
  التحويل للوضع العمودي.
- قد تختلف مدة كل جزء ببضع ثوانٍ عن الرقم المحدد بالضبط، لأن التقطيع
  السريع (copy) يلتزم بأقرب إطار مرجعي (keyframe)؛ ضبطنا فاصل الإطارات
  المرجعية على ثانيتين أثناء التحويل لتقليل هذا الفارق لأقصى حد.
- عند النشر على Streamlit Community Cloud، تأكد من وجود `packages.txt`
  يحتوي `ffmpeg` في جذر المستودع (نفس الموجود حالياً يكفي، ولا حاجة لأي
  مكتبات بايثون إضافية غير streamlit نفسها).
- لا علاقة لهذه الأداة بـ MoviePy إطلاقاً — تعتمد بالكامل على أوامر
  FFmpeg المباشرة عبر `subprocess` لأعلى سرعة وأقل استهلاك للمعالج.
        """
    )
