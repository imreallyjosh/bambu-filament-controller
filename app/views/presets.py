from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import current_user

from ..capabilities import PROFILES, bed_max_c
from ..extensions import db
from ..forms import DryingPresetForm, PresetPrinterSettingForm
from ..models import DryingPreset, DryingPresetPrinterSetting, VerificationStatus, log_event

bp = Blueprint("presets", __name__, url_prefix="/presets")

_FIELDS = ["manufacturer", "material", "product_name", "ref_air_temp_c", "ref_air_temp_max_c",
           "ref_duration_min_h", "ref_duration_max_h", "max_temp_c", "spool_requirements",
           "rotation_required", "cover_requirements", "source_reference", "verification_status", "notes"]


def _get(preset_id: int) -> DryingPreset:
    return db.session.get(DryingPreset, preset_id) or abort(404)


def _ensure_model_rows(preset: DryingPreset) -> None:
    for model in PROFILES:
        if preset.setting_for(model) is None:
            preset.printer_settings.append(DryingPresetPrinterSetting(printer_model=model))


@bp.route("/")
def index():
    presets = DryingPreset.query.order_by(DryingPreset.is_template.desc(), DryingPreset.manufacturer,
                                          DryingPreset.product_name).all()
    return render_template("presets/index.html", presets=presets)


@bp.route("/new", methods=["GET", "POST"])
@bp.route("/<int:preset_id>/edit", methods=["GET", "POST"])
def edit(preset_id=None):
    preset = _get(preset_id) if preset_id else DryingPreset(is_template=False)
    form = DryingPresetForm(obj=preset)
    if form.validate_on_submit():
        for f in _FIELDS:
            setattr(preset, f, getattr(form, f).data)
        _ensure_model_rows(preset)
        db.session.add(preset)
        db.session.flush()
        log_event("preset", f"Drying preset '{preset.label}' saved.", actor=current_user.username)
        db.session.commit()
        flash("Preset saved.", "success")
        return redirect(url_for("presets.detail", preset_id=preset.id))
    return render_template("presets/form.html", form=form, preset=preset)


@bp.route("/<int:preset_id>")
def detail(preset_id):
    preset = _get(preset_id)
    _ensure_model_rows(preset)
    db.session.commit()
    forms = {s.printer_model: PresetPrinterSettingForm(obj=s, prefix=s.printer_model) for s in preset.printer_settings}
    return render_template("presets/detail.html", preset=preset, forms=forms)


@bp.route("/<int:preset_id>/printer/<model>", methods=["POST"])
def save_printer_setting(preset_id, model):
    preset = _get(preset_id)
    if model not in PROFILES:
        abort(404)
    setting = preset.setting_for(model)
    form = PresetPrinterSettingForm(prefix=model)
    if not form.validate_on_submit():
        for field, errs in form.errors.items():
            flash(f"{model} {field}: {'; '.join(errs)}", "danger")
        return redirect(url_for("presets.detail", preset_id=preset.id))

    limits = [bed_max_c(model)]
    if preset.max_temp_c:
        limits.append(preset.max_temp_c)
    if form.max_bed_c.data:
        limits.append(form.max_bed_c.data)
    if form.bed_target_c.data and form.bed_target_c.data > min(limits):
        flash(f"{model}: bed target exceeds the lowest applicable limit ({min(limits):.0f} °C).", "danger")
        return redirect(url_for("presets.detail", preset_id=preset.id))

    before = (setting.bed_target_c, setting.max_bed_c)
    form.populate_obj(setting)
    # Changing operating temperatures always invalidates verification; re-verify in a separate save.
    if before != (setting.bed_target_c, setting.max_bed_c) and setting.verification_status == VerificationStatus.VERIFIED:
        setting.verification_status = VerificationStatus.UNVERIFIED
        flash(f"{model}: temperatures changed, so verification was reset. Save again as 'verified' "
              "only after testing the new values.", "warning")
    log_event("preset", f"{model} settings for '{preset.label}' saved.", actor=current_user.username)
    db.session.commit()
    flash(f"{model} settings saved.", "success")
    return redirect(url_for("presets.detail", preset_id=preset.id))


@bp.route("/<int:preset_id>/clone", methods=["POST"])
def clone(preset_id):
    src = _get(preset_id)
    copy = DryingPreset(is_template=False)
    for f in _FIELDS:
        setattr(copy, f, getattr(src, f))
    copy.product_name = f"{src.product_name} (custom)"
    if copy.verification_status == VerificationStatus.VERIFIED:
        copy.verification_status = VerificationStatus.REFERENCE
    for s in src.printer_settings:
        copy.printer_settings.append(DryingPresetPrinterSetting(
            printer_model=s.printer_model, bed_target_c=s.bed_target_c, expected_enclosure_c=s.expected_enclosure_c,
            max_bed_c=s.max_bed_c, fan_setting=s.fan_setting, verification_status=VerificationStatus.UNVERIFIED))
    db.session.add(copy)
    db.session.commit()
    flash("Copied to a custom preset. Printer-specific settings reset to unverified.", "info")
    return redirect(url_for("presets.edit", preset_id=copy.id))


@bp.route("/<int:preset_id>/delete", methods=["POST"])
def delete(preset_id):
    preset = _get(preset_id)
    if preset.is_template:
        flash("Templates cannot be deleted; edit or clone them instead.", "warning")
        return redirect(url_for("presets.detail", preset_id=preset.id))
    label = preset.label
    db.session.delete(preset)
    log_event("preset", f"Drying preset '{label}' deleted.", actor=current_user.username)
    db.session.commit()
    flash(f"Deleted '{label}'.", "info")
    return redirect(url_for("presets.index"))
