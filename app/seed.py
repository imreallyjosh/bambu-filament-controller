"""Initial preset templates and cited reference data.

Every number here carries its source. Manufacturer oven/dryer figures go only into the reference
fields; printer bed targets come from printer-manufacturer bed figures (or are marked as a
suggestion) and are always stored as UNVERIFIED for the printer. Data is applied only to blank
fields, so anything entered in the app is never overwritten.
"""
from .capabilities import PROFILES
from .extensions import db
from .models import AnnealingPreset, DryingPreset, DryingPresetPrinterSetting, VerificationStatus

_SPOOL_NOTE = (
    "Confirm the spool's material and heat rating before heating. Many spools are PLA, ABS, PC, "
    "or cardboard with different softening points. The bed surface beneath the spool may be far "
    "hotter than the air around the filament."
)
_COVER_NOTE = (
    "Cover the spool (e.g. a filament box with one side opened, or a heat-resistant printed cover) "
    "so heat is retained around the spool and not concentrated on one flange. Close door and lid."
)

DRYING_TEMPLATES = [
    # (manufacturer, material, product_name, notes)
    ("SUNLU", "PA6-CF", "PA6-CF", ""),
    ("Fiberon", "PA12-CF", "PA12-CF10", ""),
    ("Siraya Tech", "PPA-CF", "Fibreheart PPA-CF", ""),
    ("Generic", "PA6-CF", "Generic PA6-CF",
     "Generic: values are Bambu Lab's for this material. PA6-CF products differ; prefer a product-specific preset."),
    ("Generic", "PA12-CF", "Generic PA12-CF",
     "Generic: no PA12-CF data sheet from Bambu; values modelled on Bambu PAHT-CF. Prefer a product-specific preset."),
    ("Generic", "PPA-CF", "Generic PPA-CF",
     "Generic: values are Bambu Lab PPA-CF's. PPA grades vary widely; prefer a product-specific preset."),
    ("Generic", "PETG", "PETG", "Generic: values are Bambu Lab's for this material."),
    ("Generic", "ABS", "ABS", "Generic: values are Bambu Lab's for this material."),
    ("Generic", "ASA", "ASA", "Generic: values are Bambu Lab's for this material."),
    ("Generic", "PC", "PC", "Generic: values are Bambu Lab's for this material."),
    ("Generic", "TPU", "TPU", "Generic: values are Bambu Lab TPU for AMS (68D); softer TPUs (e.g. 95A) may differ."),
    ("Generic", "PLA", "PLA", "PLA softens at low temperatures; the spool and filament can deform if over-heated."),
]

# Templates renamed after the product name was corrected (older installs carry the old name).
RENAMED_TEMPLATES = {("Fiberon", "PPA-CF"): ("Siraya Tech", "Fibreheart PPA-CF")}

_BED_VS_AIR = (
    "Reference temperatures are drying-AIR values for an oven/dryer. A heated bed set to this value will not produce "
    "this air temperature around the spool. Printer bed targets are set separately below."
)

_BAMBU = "Bambu Lab Technical Data Sheet"
_X1_NOTE = "Bambu publishes this as an 'X1 Series Printer Heatbed' figure"


def _bed(target, hours, note):
    return {"bed_target_c": target, "drying_time_h": hours, "verification_notes": note}


def _both(target, hours, note, p1s_extra=""):
    """Same starting values for both printers, marked unverified."""
    return {
        "P1S": _bed(target, hours, f"{note} The P1S uses the same bed type but Bambu publishes no P1S figure. "
                                   f"Unverified on this printer.{(' ' + p1s_extra) if p1s_extra else ''}"),
        "X1": _bed(target, hours, f"{note} Unverified on this printer."),
    }


# Cited reference values, gathered 2026-10-08. Applied only to blank fields, so user edits are never overwritten.
SOURCED_DRYING = {
    ("SUNLU", "PA6-CF"): dict(
        ref=dict(ref_air_temp_c=80, ref_air_temp_max_c=110, ref_duration_min_h=4, ref_duration_max_h=24),
        source=("SUNLU store product page 'Nylon6+Carbon Fiber (PA6-CF)': 'Dry the filament before use: 80°C for 24 "
                "hours or 110°C for 4 hours'; drying oven 80-110 °C; keep below 20% RH sealed with desiccant. Read "
                "from the search-indexed page text (live page did not render) and matched by SUNLU's Amazon listing; "
                "confirm against your spool label. "
                "https://store.sunlu.com/products/nylon6-carbon-fiber-pa6-cf-3d-printer-filament-1kg"),
        settings=_both(90, 18, "Starting point from Bambu's PA6-CF bed figures (X1 firmware Dry filament preset "
                               "90 °C / 18 h; Bambu PA6-CF TDS 90-100 °C, 12 h)."),
    ),
    ("Fiberon", "PA12-CF10"): dict(
        ref=dict(ref_air_temp_c=100, ref_duration_min_h=10, ref_duration_max_h=10),
        source=("Polymaker TDS 'Fiberon PA12-CF10' V1.1 EN, Recommended printing conditions: 'Drying temp. and time "
                "100 °C/10H'; store and use below 20% RH. Tg 55 °C (DSC), HDT 105 °C @1.8 MPa / 131 °C @0.45 MPa "
                "(annealed specimens). https://fiberon.polymaker.com/wp-content/uploads/TDS_FIBERON-PA12-CF10_V1.1_EN.pdf"),
        settings=_both(90, 12, "Suggestion only: Polymaker publishes an oven figure, not a bed figure. Modelled on "
                               "Bambu's nylon-CF bed figures (90-100 °C, 12 h)."),
    ),
    ("Siraya Tech", "Fibreheart PPA-CF"): dict(
        ref=dict(ref_air_temp_c=100, ref_duration_min_h=4, ref_duration_max_h=6),
        source=("Siraya Tech Fibreheart PPA-CF user manual: 'If material becomes damp, dry in an oven at 100°C for 4-6 "
                "hours. Only dry if filament shows signs of moisture.' Store below 15% RH. (PPA-CF Core TDS: 80-100 °C, "
                "4-6 h; Tg 80 °C.) https://siraya.tech/pages/siraya-tech-fibreheart-ppa-cf-filament-user-manual"),
        spool="Siraya Tech does not state the spool material or its heat rating; check it before bed drying at 90 °C.",
        settings=_both(90, 12, "Suggestion only: Siraya Tech publishes an oven figure (100 °C, 4-6 h), not a bed "
                               "figure. 90 °C keeps the over-temperature trip below the P1S's 100 °C bed limit; the "
                               "longer time compensates for the lower air temperature around the spool."),
    ),
    ("Generic", "Generic PA6-CF"): dict(
        ref=dict(ref_air_temp_c=80, ref_duration_min_h=8, ref_duration_max_h=12),
        source=(f"{_BAMBU} V3.0 PA6-CF (via search excerpt; official PDF not downloadable): 'Blast Drying Oven: 80 °C, "
                "8 - 12 h; X1 Series Printer Heatbed: 90 - 100 °C, 12 h'. Bambu X1 firmware 'Dry filament' preset for "
                "PA6-CF: bed 90 °C, 18 h (as shown on an X1's touchscreen, firmware current in 2026-10)."),
        settings=_both(90, 18, f"{_X1_NOTE} (90-100 °C, 12 h); the X1 firmware's own preset uses 90 °C for 18 h."),
    ),
    ("Generic", "Generic PA12-CF"): dict(
        ref=dict(ref_air_temp_c=80, ref_duration_min_h=8, ref_duration_max_h=12),
        source=(f"No generic PA12-CF standard. Values from {_BAMBU} V2.0 PAHT-CF ('Drying Settings before Printing "
                "80 °C, 8 - 12 hours') and Bambu's PAHT-CF filament guide ('X1 Series heatbed: 90 - 100 °C, 12h'). "
                "https://bambulab.com/en-us/filament/paht-cf"),
        spool="Bambu PAHT-CF spool: PC + ABS, temperature resistance 90 °C. Check your own spool.",
        settings=_both(90, 12, f"{_X1_NOTE} for PAHT-CF (90-100 °C, 12 h); used here as a PA12-CF stand-in."),
    ),
    ("Generic", "Generic PPA-CF"): dict(
        ref=dict(ref_air_temp_c=100, ref_air_temp_max_c=140, ref_duration_min_h=8, ref_duration_max_h=12),
        source=(f"{_BAMBU} V1.0 PPA-CF: 'Blast Drying Oven: 100 - 140 °C, 8 - 12 h (within this range, a higher temp, "
                "a better drying result); X1 Series Printer Heatbed: 110 - 120 °C, 12 h (not as good as a blast drying "
                "oven)'. Spool: cardboard, 145 °C."),
        spool="Bambu PPA-CF spool: cardboard, temperature resistance 145 °C. Check your own spool.",
        settings={
            "P1S": _bed(None, None, "Not possible on the P1S: Bambu's bed figure (110-120 °C) is above the P1S's 100 °C "
                                    "bed maximum. Use an oven/dryer, or a product with a lower requirement."),
            "X1": _bed(100, 18, "Suggestion: 100 °C for 18 h. Below Bambu's X1 heatbed figure "
                                "(110-120 °C, 12 h), which is at or above the X1's 110 °C bed maximum; the longer time "
                                "compensates. Over-temperature trip at 108 °C. Unverified on this printer."),
        },
    ),
    ("Generic", "PETG"): dict(
        ref=dict(ref_air_temp_c=65, ref_duration_min_h=8, ref_duration_max_h=8),
        source=(f"{_BAMBU} V1.0 PETG HF: 'Blast Drying Oven: 65 °C, 8 h; X1 Series Printer Heatbed: 75 - 85 °C, 12 h'. "
                "https://store.bblcdn.com/3a230e260a3a47c2b0db0156e07eef91.pdf"),
        spool=("Bambu PETG HF spool: ABS, temperature resistance 70 °C, which is below Bambu's own 75-85 °C bed figure. "
               "Only bed-dry on spools rated above the bed target."),
        settings=_both(75, 12, f"{_X1_NOTE} (75-85 °C, 12 h); low end used."),
    ),
    ("Generic", "ABS"): dict(
        ref=dict(ref_air_temp_c=80, ref_duration_min_h=8, ref_duration_max_h=8),
        source=(f"{_BAMBU} V3 ABS (third-party copies; official PDF not found): 'Blast Drying Oven: 80 °C, 8 h; X1 "
                "Series Printer Heatbed: 90 - 100 °C, 12 h'. https://www.scribd.com/document/801170457/"),
        settings=_both(90, 12, f"{_X1_NOTE} (90-100 °C, 12 h); low end used."),
    ),
    ("Generic", "ASA"): dict(
        ref=dict(ref_air_temp_c=80, ref_duration_min_h=8, ref_duration_max_h=8),
        source=(f"{_BAMBU} V3.0 ASA: 'Blast Drying Oven: 80 °C, 8 h; X1 Series Printer Heatbed: 90 - 100 °C, 12 h'. "
                "https://wiki.bambulab.com/filament-acc/abs-asa-pc/6eaf4c432d1d4014a1975e55a55ed00b.pdf"),
        spool="Bambu ASA spool: PC + ABS, temperature resistance 90 °C. Check your own spool.",
        settings=_both(90, 12, f"{_X1_NOTE} (90-100 °C, 12 h); low end used (matches the 90 °C spool rating)."),
    ),
    ("Generic", "PC"): dict(
        ref=dict(ref_air_temp_c=80, ref_duration_min_h=8, ref_duration_max_h=8),
        source=(f"{_BAMBU} V3.0 PC: 'Blast Drying Oven: 80 °C, 8 h; X1 Series Printer Heatbed: 90 - 100 °C, 12 h'. "
                "https://store.bblcdn.eu/s8/default/ab03007d58814bc28a08145719b552de/Bambu_PC_Technical_Data_Sheet.pdf"),
        spool="Bambu PC spool: PC + ABS, temperature resistance 90 °C. Check your own spool.",
        settings=_both(90, 12, f"{_X1_NOTE} (90-100 °C, 12 h); low end used (matches the 90 °C spool rating)."),
    ),
    ("Generic", "TPU"): dict(
        ref=dict(ref_air_temp_c=70, ref_duration_min_h=8, ref_duration_max_h=8),
        source=(f"{_BAMBU} V1.0 TPU for AMS: 'Blast Drying Oven: 70 °C, 8 h; X1 Series Printer Heatbed: 80 - 90 °C, "
                "12 h'. https://store.bblcdn.eu/s8/default/0b83dc5419534babb4287686cb8c7375/"
                "Bambu_TPU_for_AMS_Technical_Data_Sheet.pdf"),
        spool="Bambu TPU for AMS spool: PC + ABS, temperature resistance 90 °C. Check your own spool.",
        settings=_both(80, 12, f"{_X1_NOTE} for TPU for AMS (80-90 °C, 12 h); low end used."),
    ),
    ("Generic", "PLA"): dict(
        ref=dict(ref_air_temp_c=50, ref_duration_min_h=8, ref_duration_max_h=8),
        source=(f"{_BAMBU} V3.0 PLA Basic and PLA Matte: 'Blast Drying Oven: 50 °C, 8 h; X1 Series Printer Heatbed: "
                "60 - 70 °C, 12 h'. https://wiki.bambulab.com/filament-acc/abs-asa-pc/"
                "bambu_pla_basic_technical_data_sheet.pdf"),
        spool="Bambu PLA spool: ABS, temperature resistance 70 °C. PLA (plastic) spools of other brands soften lower.",
        settings=_both(60, 12, f"{_X1_NOTE} (60-70 °C, 12 h); low end used."),
    ),
}

SOURCED_ANNEALING = [
    dict(manufacturer="Fiberon", material="PA12-CF10", anneal_temp_c=100, hold_minutes=16 * 60,
         source_reference="Polymaker TDS 'Fiberon PA12-CF10' V1.1 EN: 'Annealing temp. and time 100 °C/16H'. Annealed "
                          "parts are dry and need moisture conditioning afterwards (Polymaker wiki).",
         notes="Ramp rates are not specified by the manufacturer."),
    dict(manufacturer="SUNLU", material="PA6-CF", anneal_temp_c=80, max_temp_c=130, hold_minutes=5 * 60,
         source_reference="SUNLU PA6-CF product page (search-indexed text): 'Annealing: 80-130°C for 5-12 hours'. "
                          "Confirm against current SUNLU documentation.",
         notes="Manufacturer gives a range: 80-130 °C for 5-12 h. Stored as the low end; ramp rates not specified."),
]

_ANNEAL_RULE = ("Stored as the low end of the temperature range with the high end of the time range; the top of the "
                "range is the preset maximum. Ramp rates are not published by the manufacturer.")

# Cited annealing ranges, applied only to blank template fields: (temp_low, temp_high, hours_low, hours_high, source)
SOURCED_ANNEALING_RANGES = {
    ("Generic", "PLA"): (50, 60, 6, 12, f"{_BAMBU} V3.0 PLA Basic: 'suggested annealing temperature ... is 50 to 60 °C, "
                                       "and the time is 6 to 12 hours'."),
    ("Generic", "PETG"): (75, 80, 4, 8, f"{_BAMBU} V1.0 PETG HF: 'It's not recommended to anneal prints of PETG HF ... "
                                       "If you do want to anneal them, the suggested temperature is 75 to 80 °C, and "
                                       "the time is 4 to 8 hours.'"),
    ("Generic", "ABS"): (80, 90, 6, 12, f"{_BAMBU} ABS (third-party copies): annealing 80 to 90 °C, 6 to 12 hours."),
    ("Generic", "ASA"): (80, 90, 6, 12, f"{_BAMBU} V3.0 ASA: 'suggested annealing temperature ... is 80 to 90 °C, and "
                                       "the time is 6 to 12 hours'."),
    ("Generic", "PC"): (85, 100, 6, 12, f"{_BAMBU} V3.0 PC: 'suggested annealing temperature ... is 85 to 100 °C, and "
                                       "the time is 6 to 12 hours'."),
    ("Generic", "PA12-CF"): (80, 130, 5, 12, f"{_BAMBU} V2.0 PAHT-CF (used as a PA12-CF stand-in): 'suggested annealing "
                                            "temperature ... is 80 to 130 °C, and the time is 5 to 12 hours'. The "
                                            "printer bed limits the usable range."),
    ("Generic", "PPA-CF"): (120, 140, 6, 12, f"{_BAMBU} V1.0 PPA-CF: 'suggested annealing temperature ... is 120 to "
                                            "140 °C, and the time is 6 to 12 hours'. Above both printers' bed maximum; "
                                            "the app clamps to the printer limit, so results will be partial."),
    ("Siraya Tech", "Fibreheart PPA-CF"): (80, 100, 4, 8, "Siraya Tech Fibreheart PPA-CF user manual: 'Annealing parts "
                                                         "at 80-100°C for 4-8 hours can further enhance strength. Allow "
                                                         "parts to cool naturally before removing.'"),
    ("Polymaker", "PolyLite PLA Pro"): (50, 60, 6, 12,
                                        "Polymaker publishes NO annealing guidance for PolyLite PLA Pro (searched "
                                        "2026-10-08; a retailer spec sheet lists annealing as N/A). Its TDS gives Tg "
                                        "62 °C (DSC), HDT 57.6 °C @1.8 MPa / 59.3 °C @0.45 MPa, Vicat 62.7 °C. Values "
                                        "borrowed from Bambu Lab PLA Basic TDS V3.0 (50-60 °C, 6-12 h): a stress-relief "
                                        "anneal just below Tg. https://wiki.polymaker.com/polymaker-products/"
                                        "more-about-our-products/documents/technical-data-sheets/pla/polylite-tm-pla-pro"),
}

# Presets whose figures were borrowed from a different product: never shown as a manufacturer reference.
BORROWED_ANNEALING = {("Polymaker", "PolyLite PLA Pro")}
_PLA_PRO_NOTE = (
    "Stress relief only. Full crystallization anneals (80-120 °C) are published for PLAs formulated for annealing "
    "(e.g. Polymaker HT-PLA Pro); this toughened PLA softens above ~62 °C and would likely slump. Toughness may drop. "
    "To experiment higher, raise the preset maximum first and test on a sacrificial part."
)

ANNEALING_TEMPLATES =["PLA", "PETG", "ABS", "ASA", "PA6-CF", "PA12-CF", "PPA-CF", "PC"]

_ANNEAL_EQUIPMENT = (
    "Best results: an oven with uniform air temperature and controlled cool-down. On a printer bed (best effort): "
    "lay the part on its largest flat face, cover it to keep the air warm, and use ramps for thick parts."
)
_ANNEAL_WARNING = (
    "Annealing can cause shrinkage, warping, and sagging. Dimensional change depends on material, "
    "part geometry, infill, and print orientation. Test on sacrificial parts first."
)


def seed_presets() -> int:
    """Insert templates that don't exist yet. Returns count of rows added."""
    added = 0
    for (old_m, old_p), (new_m, new_p) in RENAMED_TEMPLATES.items():
        old = DryingPreset.query.filter_by(manufacturer=old_m, product_name=old_p, is_template=True).first()
        if old and not DryingPreset.query.filter_by(manufacturer=new_m, product_name=new_p, is_template=True).first():
            old.manufacturer, old.product_name = new_m, new_p
            old.notes = None if old.notes and "could be found" in old.notes else old.notes
    for manufacturer, material, product, notes in DRYING_TEMPLATES:
        exists = DryingPreset.query.filter_by(
            manufacturer=manufacturer, product_name=product, is_template=True
        ).first()
        if exists:
            continue
        preset = DryingPreset(
            manufacturer=manufacturer,
            material=material,
            product_name=product,
            is_template=True,
            spool_requirements=_SPOOL_NOTE,
            cover_requirements=_COVER_NOTE,
            verification_status=VerificationStatus.UNSOURCED,
            notes=notes or None,
        )
        for model in PROFILES:
            preset.printer_settings.append(
                DryingPresetPrinterSetting(printer_model=model, verification_status=VerificationStatus.UNVERIFIED)
            )
        db.session.add(preset)
        added += 1

    for material in ANNEALING_TEMPLATES:
        if AnnealingPreset.query.filter_by(manufacturer="Generic", material=material, is_template=True).first():
            continue
        db.session.add(
            AnnealingPreset(
                manufacturer="Generic",
                material=material,
                is_template=True,
                distortion_warning=_ANNEAL_WARNING,
                required_equipment=_ANNEAL_EQUIPMENT,
                verification_status=VerificationStatus.UNSOURCED,
            )
        )
        added += 1

    db.session.flush()
    added += apply_sourced_drying()

    added += apply_sourced_annealing()

    for values in SOURCED_ANNEALING:
        if AnnealingPreset.query.filter_by(manufacturer=values["manufacturer"], material=values["material"]).first():
            continue
        db.session.add(AnnealingPreset(
            is_template=True, distortion_warning=_ANNEAL_WARNING, required_equipment=_ANNEAL_EQUIPMENT,
            verification_status=VerificationStatus.REFERENCE, **values))
        added += 1
    db.session.commit()
    return added


# Notes this file wrote in earlier versions. A setting still carrying one of these is treated as untouched
# by the user, so a corrected value and note can replace it.
OBSOLETE_NOTES = {
    "Not supported by app-run bed drying: Bambu's 110-120 °C is at or above the X1's 110 °C bed maximum, leaving no "
    "over-temperature margin. Use the X1 touchscreen Dry filament preset if it offers PPA, or an oven/dryer.",
}


def apply_sourced_drying() -> int:
    """Fill blank fields of template presets from SOURCED_DRYING. Never overwrites a value the user entered."""
    changed = 0
    for (manufacturer, product), data in SOURCED_DRYING.items():
        preset = DryingPreset.query.filter_by(manufacturer=manufacturer, product_name=product, is_template=True).first()
        if preset is None:
            continue
        if preset.verification_status == VerificationStatus.UNSOURCED and preset.ref_air_temp_c is None \
                and not preset.source_reference:
            for key, value in data["ref"].items():
                setattr(preset, key, value)
            preset.source_reference = data["source"]
            preset.verification_status = VerificationStatus.REFERENCE
            preset.notes = "\n".join(filter(None, [preset.notes, _BED_VS_AIR]))
            if data.get("spool") and data["spool"] not in (preset.spool_requirements or ""):
                preset.spool_requirements = "\n".join(filter(None, [data["spool"], preset.spool_requirements]))
            changed += 1
        for model, values in data["settings"].items():
            setting = preset.setting_for(model)
            if setting is None:
                setting = DryingPresetPrinterSetting(printer_model=model)
                preset.printer_settings.append(setting)
            untouched = setting.bed_target_c is None and (not setting.verification_notes
                                                          or setting.verification_notes in OBSOLETE_NOTES)
            for key in ("bed_target_c", "drying_time_h"):
                if getattr(setting, key) is None and values[key] is not None:
                    setattr(setting, key, values[key])
                    changed += 1
            if untouched:
                setting.verification_notes = values["verification_notes"]
                setting.verification_status = VerificationStatus.UNVERIFIED
    return changed


def apply_sourced_annealing() -> int:
    """Create/fill annealing templates from SOURCED_ANNEALING_RANGES. Never overwrites user-entered values."""
    changed = 0
    for (manufacturer, material), (t_lo, t_hi, h_lo, h_hi, source) in SOURCED_ANNEALING_RANGES.items():
        preset = AnnealingPreset.query.filter_by(manufacturer=manufacturer, material=material, is_template=True).first()
        if preset is None:
            preset = AnnealingPreset(manufacturer=manufacturer, material=material, is_template=True,
                                     distortion_warning=_ANNEAL_WARNING, required_equipment=_ANNEAL_EQUIPMENT,
                                     verification_status=VerificationStatus.UNSOURCED)
            db.session.add(preset)
        if preset.anneal_temp_c is not None or preset.source_reference:
            continue
        preset.anneal_temp_c, preset.max_temp_c, preset.hold_minutes = t_lo, t_hi, h_hi * 60
        preset.source_reference = source
        borrowed = (manufacturer, material) in BORROWED_ANNEALING
        preset.notes = "\n".join(filter(None, [preset.notes, f"Range {t_lo}-{t_hi} °C, {h_lo}-{h_hi} h. {_ANNEAL_RULE}",
                                                _PLA_PRO_NOTE if borrowed else None]))
        preset.verification_status = VerificationStatus.UNVERIFIED if borrowed else VerificationStatus.REFERENCE
        changed += 1
    return changed
