from __future__ import annotations

from dataclasses import dataclass, field

from .ui_dump import UINode


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


@dataclass(slots=True)
class GoPayPageSpec:
    page_id: str
    title: str
    any_text: list[str] = field(default_factory=list)
    any_desc: list[str] = field(default_factory=list)
    all_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    notes: str = ""
    next_candidate: str = ""


@dataclass(slots=True)
class GoPayPageMatch:
    spec: GoPayPageSpec
    score: int
    matched_terms: list[str]


KNOWN_GOPAY_PAGES: list[GoPayPageSpec] = [
    GoPayPageSpec(
        page_id="landing_intro",
        title="Landing Intro",
        any_desc=["Masukkan nomor HP-mu"],
        all_keywords=["Bahasa Indonesia", "Help"],
        exclude_keywords=["Selamat datang"],
        notes="GoPay landing page before phone-number entry.",
        next_candidate="Primary CTA: Masukkan nomor HP-mu",
    ),
    GoPayPageSpec(
        page_id="location_permission_intro",
        title="Location Permission Intro",
        any_desc=[
            "Location Permission Screen",
            "Izinkan akses lokasi biar GoPay kamu extra aman",
            "Perlindungan dari penipuan",
            "Promo sekitarmu",
            "Data lokasi aman",
            "Oke, lanjut",
            "Nanti aja",
        ],
        any_text=[
            "Izinkan akses lokasi biar GoPay kamu extra aman",
            "Perlindungan dari penipuan",
            "Promo sekitarmu",
            "Data lokasi aman",
            "Oke, lanjut",
            "Nanti aja",
        ],
        all_keywords=[],
        exclude_keywords=[],
        notes="First-launch GoPay location permission intro screen. Decline it by tapping Nanti aja.",
        next_candidate="Tap Nanti aja to reject the location prompt",
    ),
    GoPayPageSpec(
        page_id="phone_input",
        title="Phone Number Input",
        any_desc=["Selamat datang di GoPay", "Nomor HP", "Country code"],
        all_keywords=["Bahasa Indonesia", "Lanjut"],
        exclude_keywords=["Masukkan nomor HP-mu", "Signup Terms Summary", "Location Permission Screen", "Nanti aja", "Izinkan akses lokasi"],
        notes="Phone number entry page with country code selector. "
              "EditText at [138,288][1026,324], use ADBKeyboard broadcast to input.",
        next_candidate="Enter phone number via ADBKeyboard, then tap Lanjut",
    ),
    GoPayPageSpec(
        page_id="signup_terms",
        title="Signup Terms Summary",
        any_desc=["Signup Terms Summary", "Penting sebelum kamu lanjut"],
        all_keywords=[],
        exclude_keywords=[],
        notes="Terms acceptance page after phone entry. Tap Lanjut at bottom to accept.",
        next_candidate="Tap Lanjut to accept terms and proceed to OTP",
    ),
    GoPayPageSpec(
        page_id="verification_method",
        title="Verification Method Selection",
        any_desc=["Pilih metode verifikasi", "OTP via WhatsApp", "OTP via SMS"],
        any_text=["Pilih metode verifikasi", "OTP via WhatsApp", "OTP via SMS"],
        all_keywords=[],
        exclude_keywords=[],
        notes="Verification method chooser shown before OTP delivery.",
        next_candidate="Available options: OTP via WhatsApp or OTP via SMS",
    ),
    GoPayPageSpec(
        page_id="otp_input_whatsapp",
        title="OTP Input WhatsApp",
        any_desc=["Cek WhatsApp, ya", "Buka WhatsApp", "Coba Metode Lainnya", "Kirim Ulang"],
        any_text=["Cek WhatsApp, ya", "Buka WhatsApp", "Coba Metode Lainnya", "Kirim Ulang"],
        all_keywords=[],
        exclude_keywords=["Masukkan OTP yang kami SMS"],
        notes="Post-PIN WhatsApp OTP page. Switch to SMS by tapping Coba Metode Lainnya.",
        next_candidate="Tap Coba Metode Lainnya, then choose OTP via SMS",
    ),
    GoPayPageSpec(
        page_id="otp_input",
        title="OTP Input",
        any_desc=["Masukkan OTP yang kami SMS", "OTP, wajib Bidang Masukan", "Kirim Ulang", "Coba Metode Lainnya"],
        any_text=["Masukkan OTP", "Kirim Ulang", "Coba Metode Lainnya"],
        all_keywords=[],
        exclude_keywords=["Cek WhatsApp, ya", "Buka WhatsApp"],
        notes="OTP code entry page after verification method selection.",
        next_candidate="Enter the received OTP into the input field",
    ),
    GoPayPageSpec(
        page_id="profile_name_input",
        title="Profile Name Input",
        any_desc=["Isi data diri dulu, ya", "Nama", "Masukkan namamu", "Buat akun"],
        any_text=["Isi data diri dulu, ya", "Nama", "Masukkan namamu", "Buat akun"],
        all_keywords=[],
        exclude_keywords=[],
        notes="Post-OTP profile page asking for the account holder name before account creation.",
        next_candidate="Enter a generated account name and tap Buat akun",
    ),
    GoPayPageSpec(
        page_id="home_dashboard",
        title="Home Dashboard",
        any_desc=["Top Up Games", "Eksplor fitur GoPay", "Transfer & Terima", "Spesial cuma buat kamu"],
        any_text=["Top Up Games", "Eksplor fitur GoPay", "Transfer & Terima", "Spesial cuma buat kamu"],
        all_keywords=[],
        exclude_keywords=["Pengaturan & keamanan", "Perlindungan akun"],
        notes="Main GoPay home dashboard shown after account creation.",
        next_candidate="Tap the Profil tab in the bottom navigation",
    ),
    GoPayPageSpec(
        page_id="profile_dashboard",
        title="Profile Dashboard",
        any_desc=["Pengaturan & keamanan", "Perlindungan akun", "Pengaturan akun & aplikasi", "Bantuan"],
        any_text=["Pengaturan & keamanan", "Perlindungan akun", "Pengaturan akun & aplikasi", "Bantuan"],
        all_keywords=[],
        exclude_keywords=[],
        notes="Profile and security page reached from the Profil bottom tab.",
        next_candidate="Tap the Perlindungan akun section to open the protection checklist",
    ),
    GoPayPageSpec(
        page_id="protection_overview",
        title="Protection Overview",
        any_desc=[
            "0/4 langkah tuntas",
            "Izin lokasi",
            "Pasang PIN",
            "Verifikasi Email",
            "Upgrade ke GoPay Plus",
        ],
        any_text=[
            "0/4 langkah tuntas",
            "Izin lokasi",
            "Pasang PIN",
            "Verifikasi Email",
            "Upgrade ke GoPay Plus",
        ],
        all_keywords=[],
        exclude_keywords=[],
        notes="Account protection checklist page opened from the profile security card.",
        next_candidate="Tap Pasang PIN in the checklist",
    ),
    GoPayPageSpec(
        page_id="username_input",
        title="Username Input",
        any_desc=["Buat username", "Username", "nama pengguna", "Create username"],
        any_text=["Buat username", "Username", "nama pengguna", "Create username"],
        all_keywords=[],
        exclude_keywords=[],
        notes="Username setup page shown after OTP verification.",
        next_candidate="Enter a generated username and continue",
    ),
    GoPayPageSpec(
        page_id="pin_input",
        title="PIN Input",
        any_desc=["Pasang PIN", "Tips bikin PIN yang aman", "PIN GoPay bikin bayar-bayar", "Buat PIN", "Masukkan PIN", "Create PIN", "6 digit PIN"],
        any_text=["Pasang PIN", "Tips bikin PIN yang aman", "PIN GoPay bikin bayar-bayar", "Buat PIN", "Masukkan PIN", "Create PIN", "6 digit PIN"],
        all_keywords=[],
        exclude_keywords=[],
        notes="PIN creation page shown after account profile setup.",
        next_candidate="Enter the generated PIN",
    ),
    GoPayPageSpec(
        page_id="pin_confirm",
        title="PIN Confirm",
        any_desc=["Konfirmasi PIN", "Confirm PIN", "Masukkan ulang PIN", "Ulangi PIN"],
        any_text=["Konfirmasi PIN", "Confirm PIN", "Masukkan ulang PIN", "Ulangi PIN"],
        all_keywords=[],
        exclude_keywords=[],
        notes="PIN confirmation page shown after PIN entry.",
        next_candidate="Re-enter the same PIN to confirm",
    ),
]


def collect_node_strings(nodes: list[UINode]) -> tuple[list[str], list[str], str]:
    texts = [_normalize(node.text) for node in nodes if node.text]
    descs = [_normalize(node.content_desc) for node in nodes if node.content_desc]
    haystack = " ".join(texts + descs)
    return texts, descs, haystack


def detect_gopay_page(nodes: list[UINode], ocr_lines: list[str] | None = None) -> GoPayPageMatch | None:
    texts, descs, haystack = collect_node_strings(nodes)
    ocr_texts: list[str] = []
    if ocr_lines:
        ocr_texts = [_normalize(line) for line in ocr_lines if line.strip()]
        haystack = " ".join([haystack, *ocr_texts]).strip()

    best_match: GoPayPageMatch | None = None
    for spec in KNOWN_GOPAY_PAGES:
        matched_terms: list[str] = []
        score = 0
        matched_primary = False

        for term in spec.any_text:
            normalized = _normalize(term)
            if normalized and (
                any(normalized in text for text in texts)
                or any(normalized in text for text in ocr_texts)
            ):
                matched_terms.append(term)
                score += 2
                matched_primary = True

        for term in spec.any_desc:
            normalized = _normalize(term)
            if normalized and any(normalized in desc for desc in descs):
                matched_terms.append(term)
                score += 3
                matched_primary = True

        all_keywords_match = True
        for term in spec.all_keywords:
            normalized = _normalize(term)
            if normalized not in haystack:
                all_keywords_match = False
                break
            matched_terms.append(term)
            score += 1

        for term in spec.exclude_keywords:
            normalized = _normalize(term)
            if normalized and normalized in haystack:
                all_keywords_match = False
                break

        if (spec.any_text or spec.any_desc) and not matched_primary:
            continue
        if spec.all_keywords and not all_keywords_match:
            continue
        if spec.exclude_keywords and not all_keywords_match:
            continue

        candidate = GoPayPageMatch(spec=spec, score=score, matched_terms=matched_terms)
        if best_match is None or candidate.score > best_match.score:
            best_match = candidate

    return best_match


def actionable_nodes(nodes: list[UINode]) -> list[UINode]:
    return [
        node
        for node in nodes
        if node.clickable and node.enabled and (node.text or node.content_desc)
    ]
