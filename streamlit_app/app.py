"""
streamlit_app/app.py
--------------------
GameSoul — Find Your Next Game by How You Want to Feel.
Full Streamlit frontend with all four input modes.
"""

import uuid
import json
import os
import time
import requests
import streamlit as st

API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")

st.set_page_config(
    page_title="GameSoul — Find Your Next Game",
    page_icon="🎮",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Styles ────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');

html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

.hero-title {
    font-size: 3.5rem;
    font-weight: 700;
    background: linear-gradient(135deg, #a855f7, #3b82f6, #06b6d4);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    line-height: 1.1;
    margin-bottom: 0.5rem;
}
.hero-sub {
    font-size: 1.2rem;
    color: #94a3b8;
    margin-bottom: 2.5rem;
}
.mode-card {
    background: #1e1e2e;
    border: 1px solid #313244;
    border-radius: 16px;
    padding: 1.5rem;
    cursor: pointer;
    transition: all 0.2s;
    text-align: center;
    min-height: 130px;
}
.mode-card:hover { border-color: #a855f7; transform: translateY(-2px); }
.mode-card.selected { border-color: #a855f7; background: #2d1b69; }
.mode-icon { font-size: 2.5rem; margin-bottom: 0.5rem; }
.mode-label { font-size: 1rem; font-weight: 600; color: #e2e8f0; }
.mode-desc { font-size: 0.8rem; color: #64748b; margin-top: 0.25rem; }
.game-card {
    background: #1e1e2e;
    border: 1px solid #313244;
    border-radius: 12px;
    padding: 1.25rem;
    margin-bottom: 1rem;
}
.game-card h3 { margin: 0 0 0.25rem 0; color: #e2e8f0; }
.game-card .score { color: #a855f7; font-size: 1.5rem; font-weight: 700; }
.game-card .explanation { color: #94a3b8; font-size: 0.9rem; margin-top: 0.5rem; }
.dim-bar { background: #313244; border-radius: 4px; height: 8px; margin: 3px 0; }
.dim-fill { background: linear-gradient(90deg, #a855f7, #3b82f6); height: 100%; border-radius: 4px; }
.dim-label { font-size: 0.72rem; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; }
.section-header { font-size: 1.4rem; font-weight: 600; color: #e2e8f0; margin: 2rem 0 1rem 0; }
.image-card {
    border: 2px solid transparent;
    border-radius: 12px;
    overflow: hidden;
    cursor: pointer;
    transition: border-color 0.2s;
    position: relative;
}
.image-card.selected { border-color: #a855f7; }
.sound-chip {
    display: inline-block;
    background: #1e1e2e;
    border: 1px solid #313244;
    border-radius: 24px;
    padding: 0.5rem 1rem;
    cursor: pointer;
    margin: 0.25rem;
    transition: all 0.2s;
    font-size: 0.9rem;
}
.sound-chip.selected { border-color: #a855f7; background: #2d1b69; color: #e2e8f0; }
</style>
""", unsafe_allow_html=True)

# ── Session State ─────────────────────────────────────────────────────────────
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "mode" not in st.session_state:
    st.session_state.mode = None
if "results" not in st.session_state:
    st.session_state.results = None
if "visual_selected" not in st.session_state:
    st.session_state.visual_selected = []
if "sound_selected" not in st.session_state:
    st.session_state.sound_selected = []
if "loved_game" not in st.session_state:
    st.session_state.loved_game = None
if "hated_game" not in st.session_state:
    st.session_state.hated_game = None


# ── API helpers ───────────────────────────────────────────────────────────────
def api_post(endpoint: str, payload: dict):
    try:
        r = requests.post(f"{API_BASE}{endpoint}", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        return _mock_results(payload.get("session_id", str(uuid.uuid4())))
    except Exception as e:
        st.error(f"API error: {e}")
        return None


def api_get(endpoint: str, params: dict = None):
    try:
        r = requests.get(f"{API_BASE}{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def _mock_results(session_id: str):
    """Demo data when API is not running."""
    return {
        "session_id": session_id,
        "recommendation_id": 1,
        "query_vector": {d: 5.0 for d in ["pace","tension","agency","warmth","scale","beauty","dread","wonder","rivalry"]},
        "input_mode": "demo",
        "recommendations": [
            {
                "game_id": 1, "name": "Hades", "similarity_score": 0.94,
                "explanation": "Matches your desire for high-stakes tension and player agency.",
                "cover_url": None,
                "emotion_vector": {"pace":8,"tension":9,"agency":9,"warmth":4,"scale":3,"beauty":9,"dread":4,"wonder":7,"rivalry":1},
            },
            {
                "game_id": 2, "name": "Stardew Valley", "similarity_score": 0.87,
                "explanation": "Closely mirrors your emotional target for warmth and calm.",
                "cover_url": None,
                "emotion_vector": {"pace":1,"tension":1,"agency":8,"warmth":9,"scale":2,"beauty":8,"dread":0,"wonder":6,"rivalry":0},
            },
            {
                "game_id": 3, "name": "Outer Wilds", "similarity_score": 0.82,
                "explanation": "Matches your desire for wonder and discovery.",
                "cover_url": None,
                "emotion_vector": {"pace":3,"tension":5,"agency":7,"warmth":5,"scale":9,"beauty":9,"dread":3,"wonder":10,"rivalry":0},
            },
            {
                "game_id": 4, "name": "Celeste", "similarity_score": 0.79,
                "explanation": "Shares your target for beauty and meaningful agency.",
                "cover_url": None,
                "emotion_vector": {"pace":6,"tension":7,"agency":9,"warmth":7,"scale":3,"beauty":9,"dread":2,"wonder":6,"rivalry":0},
            },
            {
                "game_id": 5, "name": "Into the Breach", "similarity_score": 0.76,
                "explanation": "Matches high agency under controlled pressure.",
                "cover_url": None,
                "emotion_vector": {"pace":5,"tension":8,"agency":10,"warmth":3,"scale":5,"beauty":6,"dread":3,"wonder":5,"rivalry":0},
            },
        ],
    }


# ── Hero ──────────────────────────────────────────────────────────────────────
col_left, col_right = st.columns([2, 1])
with col_left:
    st.markdown('<div class="hero-title">GameSoul</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="hero-sub">Find your next game by how you want to <em>feel</em>, not what genre you want to play.</div>',
        unsafe_allow_html=True,
    )

# ── Mode Selector ─────────────────────────────────────────────────────────────
st.markdown('<div class="section-header">How do you want to find your game today?</div>', unsafe_allow_html=True)

modes = [
    ("text",   "💬", "Describe a feeling",      "Tell us in your own words"),
    ("visual", "🖼️",  "Visual mood",             "Pick images that match your vibe"),
    ("sound",  "🎵", "Sound check",              "Pick audio clips that resonate"),
    ("anchor", "🎯", "Love it / Hate it",        "Use two games as anchors"),
]

cols = st.columns(4)
for col, (mode_id, icon, label, desc) in zip(cols, modes):
    with col:
        selected = st.session_state.mode == mode_id
        cls = "mode-card selected" if selected else "mode-card"
        clicked = st.button(
            f"{icon}\n\n**{label}**\n\n{desc}",
            key=f"mode_{mode_id}",
            use_container_width=True,
            type="primary" if selected else "secondary",
        )
        if clicked:
            st.session_state.mode = mode_id
            st.session_state.results = None
            st.rerun()

st.divider()

# ── Input Mode UIs ────────────────────────────────────────────────────────────

if st.session_state.mode == "text":
    st.markdown('<div class="section-header">💬 Describe how you want to feel</div>', unsafe_allow_html=True)
    st.caption("Be specific about emotions, not genres. 'I want to feel alert and in control under pressure' works great.")

    col1, col2 = st.columns([3, 1])
    with col1:
        text_input = st.text_area(
            "Your feeling",
            placeholder="e.g. I want to feel calm and creative, like I'm building something meaningful without any pressure or threat...",
            height=120,
            label_visibility="collapsed",
        )
    with col2:
        st.markdown("**Examples:**")
        examples = [
            "Alert and in control under pressure",
            "Peaceful, slow, no stakes",
            "Terrified but curious",
            "Epic and powerful, like I'm a legend",
        ]
        for ex in examples:
            if st.button(ex, key=f"ex_{ex[:10]}", use_container_width=True):
                st.session_state["text_prefill"] = ex

    use_openai = st.toggle("Use GPT-4o (slower, more accurate)", value=False)

    if st.button("🔍 Find My Games", type="primary", disabled=not text_input):
        with st.spinner("Reading your emotional state..."):
            result = api_post("/recommend/text", {
                "text": text_input,
                "session_id": st.session_state.session_id,
                "use_openai": use_openai,
            })
            if result:
                st.session_state.results = result
                st.rerun()


elif st.session_state.mode == "visual":
    st.markdown('<div class="section-header">🖼️ What visual pulls you in today?</div>', unsafe_allow_html=True)
    st.caption("Select all images that match how you're feeling right now. No wrong answers.")

    MOOD_IMAGES = [
        ("rainy_window",    "🌧️ Rainy window",      "#667eea"),
        ("lone_mountain",   "⛰️ Lone mountain",      "#764ba2"),
        ("forest_campfire", "🔥 Forest campfire",    "#f093fb"),
        ("neon_city",       "🌃 Neon city",          "#4facfe"),
        ("arena_crowd",     "🏟️ Arena crowd",         "#f5576c"),
        ("dark_corridor",   "🕳️ Dark corridor",       "#2d3436"),
        ("deep_space",      "🌌 Deep space",          "#a29bfe"),
        ("storm_at_sea",    "🌊 Storm at sea",        "#00cec9"),
        ("crowded_market",  "🏪 Crowded market",      "#fd79a8"),
        ("cozy_library",    "📚 Cozy library",         "#55efc4"),
        ("empty_desert",    "🏜️ Empty desert",        "#fdcb6e"),
        ("sunrise_peak",    "🌄 Sunrise peak",         "#e17055"),
    ]

    selected = st.session_state.visual_selected
    cols = st.columns(4)
    for i, (img_id, label, color) in enumerate(MOOD_IMAGES):
        with cols[i % 4]:
            is_sel = img_id in selected
            bg = color if is_sel else "#1e1e2e"
            border = "#a855f7" if is_sel else "#313244"
            st.markdown(
                f"""<div style="background:{bg};border:2px solid {border};border-radius:12px;
                    padding:1.5rem;text-align:center;margin-bottom:0.5rem;font-size:2rem">
                    {label.split()[0]}<br>
                    <span style="font-size:0.8rem;color:#e2e8f0">{" ".join(label.split()[1:])}</span>
                </div>""",
                unsafe_allow_html=True,
            )
            if st.button("Select" if not is_sel else "✓ Selected", key=f"img_{img_id}", use_container_width=True):
                if is_sel:
                    st.session_state.visual_selected.remove(img_id)
                else:
                    st.session_state.visual_selected.append(img_id)
                st.rerun()

    st.write(f"**Selected:** {len(selected)} image(s)")
    if st.button("🔍 Find My Games", type="primary", disabled=len(selected) == 0):
        with st.spinner("Translating your visual mood..."):
            result = api_post("/recommend/visual", {
                "selected_image_ids": selected,
                "session_id": st.session_state.session_id,
            })
            if result:
                st.session_state.results = result
                st.rerun()


elif st.session_state.mode == "sound":
    st.markdown('<div class="section-header">🎵 Which sounds match your current state?</div>', unsafe_allow_html=True)
    st.caption("Imagine these playing right now. Pick 1–3 that resonate.")

    SOUND_CLIPS = [
        ("rain_ambient",   "🌧️ Gentle rain",         "Soft patter on glass, contemplative"),
        ("battle_drums",   "🥁 War drums",            "Driving, adrenaline, battle-ready"),
        ("synthwave",      "🎹 Synthwave",            "Neon, retro-future, flowing"),
        ("nature_birds",   "🐦 Forest birdsong",      "Warm, alive, unhurried"),
        ("deep_space_hum", "🌌 Deep space drone",     "Vast, mysterious, weightless"),
    ]

    selected = st.session_state.sound_selected

    for clip_id, label, desc in SOUND_CLIPS:
        is_sel = clip_id in selected
        with st.container():
            c1, c2, c3 = st.columns([1, 4, 1])
            with c1:
                st.markdown(f"<div style='font-size:2.5rem;text-align:center'>{label.split()[0]}</div>", unsafe_allow_html=True)
            with c2:
                st.markdown(f"**{' '.join(label.split()[1:])}**")
                st.caption(desc)
            with c3:
                if st.button("✓" if is_sel else "Pick", key=f"snd_{clip_id}", use_container_width=True, type="primary" if is_sel else "secondary"):
                    if is_sel:
                        st.session_state.sound_selected.remove(clip_id)
                    else:
                        st.session_state.sound_selected.append(clip_id)
                    st.rerun()
        st.divider()

    if st.button("🔍 Find My Games", type="primary", disabled=len(selected) == 0):
        with st.spinner("Tuning to your frequency..."):
            result = api_post("/recommend/sound", {
                "selected_clip_ids": selected,
                "session_id": st.session_state.session_id,
            })
            if result:
                st.session_state.results = result
                st.rerun()


elif st.session_state.mode == "anchor":
    st.markdown('<div class="section-header">🎯 Use two games as your emotional anchors</div>', unsafe_allow_html=True)
    st.caption("GameSoul will find the emotional contrast and search for games that have what you loved, without what you hated.")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### 💚 A game you LOVED")
        loved_search = st.text_input("Search for a game", key="loved_search", placeholder="Type a game name...")
        if loved_search:
            games = api_get("/games/search", {"q": loved_search, "limit": 5})
            if games:
                for g in games:
                    if st.button(g["name"], key=f"loved_{g['id']}", use_container_width=True):
                        st.session_state.loved_game = g
                        st.rerun()
        if st.session_state.loved_game:
            st.success(f"✓ {st.session_state.loved_game['name']}")

    with col2:
        st.markdown("### 🔴 A game you DISLIKED")
        hated_search = st.text_input("Search for a game", key="hated_search", placeholder="Type a game name...")
        if hated_search:
            games = api_get("/games/search", {"q": hated_search, "limit": 5})
            if games:
                for g in games:
                    if st.button(g["name"], key=f"hated_{g['id']}", use_container_width=True):
                        st.session_state.hated_game = g
                        st.rerun()
        if st.session_state.hated_game:
            st.error(f"✗ {st.session_state.hated_game['name']}")

    can_search = st.session_state.loved_game and st.session_state.hated_game
    if st.button("🔍 Find My Games", type="primary", disabled=not can_search):
        with st.spinner("Computing emotional contrast..."):
            result = api_post("/recommend/anchor", {
                "loved_game_id": st.session_state.loved_game["id"],
                "hated_game_id": st.session_state.hated_game["id"],
                "session_id": st.session_state.session_id,
            })
            if result:
                st.session_state.results = result
                st.rerun()


# ── Results ────────────────────────────────────────────────────────────────────

if st.session_state.results:
    results = st.session_state.results
    st.divider()
    st.markdown('<div class="section-header">🎮 Your Games</div>', unsafe_allow_html=True)

    # Emotion vector radar-ish display
    with st.expander("Your emotional target vector", expanded=False):
        qv = results.get("query_vector", {})
        dims = ["pace","tension","agency","warmth","scale","beauty","dread","wonder","rivalry"]
        cols = st.columns(9)
        for col, d in zip(cols, dims):
            with col:
                val = qv.get(d, 5)
                st.metric(d.capitalize(), f"{val:.1f}")

    recs = results.get("recommendations", [])
    for i, rec in enumerate(recs):
        with st.container():
            c1, c2 = st.columns([3, 1])
            with c1:
                st.markdown(f"### #{i+1} {rec['name']}")
                score_pct = int(rec['similarity_score'] * 100)
                st.markdown(
                    f"<div style='font-size:1.8rem;font-weight:700;color:#a855f7'>{score_pct}% match</div>",
                    unsafe_allow_html=True,
                )
                st.caption(rec.get("explanation", ""))

                # Dimension bars
                ev = rec.get("emotion_vector", {})
                dim_cols = st.columns(9)
                for dc, d in zip(dim_cols, ["pace","tension","agency","warmth","scale","beauty","dread","wonder","rivalry"]):
                    with dc:
                        v = ev.get(d, 5)
                        st.markdown(
                            f"""<div class="dim-label">{d[:3]}</div>
                            <div class="dim-bar"><div class="dim-fill" style="width:{v*10}%"></div></div>
                            <div style="font-size:0.7rem;color:#64748b;text-align:center">{v:.0f}</div>""",
                            unsafe_allow_html=True,
                        )

            with c2:
                st.markdown("**Rate this**")
                cols_r = st.columns(2)
                with cols_r[0]:
                    if st.button("👍", key=f"up_{rec['game_id']}_{i}", use_container_width=True):
                        requests.post(f"{API_BASE}/rate", json={
                            "session_id": results["session_id"],
                            "recommendation_id": results["recommendation_id"],
                            "game_id": rec["game_id"],
                            "thumbs_up": True,
                        })
                        st.success("Thanks!")
                with cols_r[1]:
                    if st.button("👎", key=f"down_{rec['game_id']}_{i}", use_container_width=True):
                        requests.post(f"{API_BASE}/rate", json={
                            "session_id": results["session_id"],
                            "recommendation_id": results["recommendation_id"],
                            "game_id": rec["game_id"],
                            "thumbs_up": False,
                        })
                        st.info("Noted!")

                rating = st.select_slider(
                    "Stars", options=[1,2,3,4,5], value=3,
                    key=f"stars_{rec['game_id']}_{i}",
                    label_visibility="collapsed",
                )
                if st.button("Rate", key=f"rate_{rec['game_id']}_{i}", use_container_width=True):
                    requests.post(f"{API_BASE}/rate", json={
                        "session_id": results["session_id"],
                        "recommendation_id": results["recommendation_id"],
                        "game_id": rec["game_id"],
                        "rating": rating,
                    })
                    st.success(f"{'⭐' * rating}")

        st.divider()

    if st.button("🔄 Try again", use_container_width=True):
        st.session_state.results = None
        st.rerun()


# ── Footer ─────────────────────────────────────────────────────────────────────
st.markdown("""
<div style="text-align:center;color:#475569;font-size:0.8rem;padding:2rem 0 1rem">
    GameSoul — 50,000+ games mapped across 9 emotional dimensions
    <br>Session: <code style="color:#64748b">{}</code>
</div>
""".format(st.session_state.session_id[:8] + "..."), unsafe_allow_html=True)
