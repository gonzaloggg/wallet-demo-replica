import React, { useState, useEffect, DragEvent, ChangeEvent } from "react";

/**
 * Estado tipado en TypeScript para la configuración integral de la Tarjeta/Campaña
 * Compatible con Apple Wallet (.pkpass) y Google Wallet (LoyaltyObject / LoyaltyClass).
 */
export interface CampaignConfig {
  // 1. Pestaña de Marca y Diseño
  shopName: string;
  backgroundColor: string;
  foregroundColor: string;
  labelColor: string;
  logoDataUrl: string;
  stripDataUrl: string;
  useCustomStripBg: boolean;

  // 2. Pestaña de Reglas del Programa
  stampGoal: number;
  rewardText: string;
  termsText: string;
  menuUrl: string;

  // 3. Pestaña de Ubicación (Geovalla GPS)
  lat: number;
  lng: number;
  relevantText: string;
}

export interface ImageValidationState {
  valid: boolean;
  width: number;
  height: number;
  ratio: number;
  message: string;
  severity: "ok" | "warn" | "idle";
}

export type CampaignTabKey = "brand" | "rules" | "location";

const DEFAULT_CAMPAIGN_CONFIG: CampaignConfig = {
  shopName: "Maishas Café",
  backgroundColor: "#12100E",
  foregroundColor: "#F5F2EB",
  labelColor: "#D4AF37",
  logoDataUrl: "/api/campaign/logo.png",
  stripDataUrl: "",
  useCustomStripBg: false,
  stampGoal: 10,
  rewardText: "¡1 Café o Bebida Gratis a elección!",
  termsText:
    "1. Acumula 1 sello por cada visita o compra participante.\n2. Al completar la meta de sellos, canjea 1 bebida de cortesía en mostrador.\n3. Tarjeta personal e intransferible.",
  menuUrl: "https://wa.me/526421127306",
  lat: 27.0706,
  lng: -109.4437,
  relevantText:
    "☕️ ¡Estás cerca de Maishas Café! Pasa por tu café favorito y suma tu sello de hoy ✨",
};

export const CampaignAdminDashboard: React.FC<{
  initialConfig?: Partial<CampaignConfig>;
  endpointUrl?: string;
  onSaved?: (updated: CampaignConfig) => void;
}> = ({
  initialConfig,
  endpointUrl = "/api/campaign-settings",
  onSaved,
}) => {
  const [config, setConfig] = useState<CampaignConfig>({
    ...DEFAULT_CAMPAIGN_CONFIG,
    ...initialConfig,
  });
  const [activeTab, setActiveTab] = useState<CampaignTabKey>("brand");
  const [previewStamps, setPreviewStamps] = useState<number>(3);
  const [showBackSide, setShowBackSide] = useState<boolean>(false);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [saveFeedback, setSaveFeedback] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);

  const [logoValidation, setLogoValidation] = useState<ImageValidationState>({
    valid: true,
    width: 660,
    height: 660,
    ratio: 1,
    message: "Proporción cuadrada 1:1 óptima para Apple y Google Wallet",
    severity: "ok",
  });

  const [stripValidation, setStripValidation] = useState<ImageValidationState>({
    valid: true,
    width: 0,
    height: 0,
    ratio: 3,
    message: "Usando generador dinámico de sellos en alta resolución (750×250 px)",
    severity: "idle",
  });

  // Cargar configuración actual desde el backend al montar el componente
  useEffect(() => {
    fetch(endpointUrl, { credentials: "same-origin" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data && typeof data === "object") {
          setConfig((prev) => ({ ...prev, ...data }));
        }
      })
      .catch(() => {});
  }, [endpointUrl]);

  // Asegurar que los sellos de prueba no excedan la meta configurada
  useEffect(() => {
    if (previewStamps > config.stampGoal) {
      setPreviewStamps(config.stampGoal);
    }
  }, [config.stampGoal, previewStamps]);

  const updateField = <K extends keyof CampaignConfig>(
    key: K,
    value: CampaignConfig[K]
  ) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
    setSaveFeedback(null);
  };

  // Validación de proporciones de imagen mediante HTMLImageElement
  const handleImageFile = (file: File, target: "logo" | "strip") => {
    if (!file || !file.type.startsWith("image/")) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      const dataUrl = String(ev.target?.result || "");
      if (!dataUrl) return;
      const img = new Image();
      img.onload = () => {
        const w = img.naturalWidth || img.width;
        const h = img.naturalHeight || img.height;
        const ratio = Number((w / Math.max(1, h)).toFixed(2));

        if (target === "logo") {
          // Apple Wallet y Google Wallet recomiendan entre 1:1 (cuadrado >= 660x660) y 3.2:1 (logo horizontal)
          const isGoodRatio = ratio >= 0.85 && ratio <= 3.5;
          setLogoValidation({
            valid: isGoodRatio,
            width: w,
            height: h,
            ratio,
            severity: isGoodRatio ? "ok" : "warn",
            message: isGoodRatio
              ? `✅ Dimensiones válidas (${w}×${h}px · relación ${ratio}:1)`
              : `⚠️ Relación ${ratio}:1 (${w}×${h}px). Se recomienda 1:1 cuadrado (ej. 660×660px) o hasta 3:1.`,
          });
          updateField("logoDataUrl", dataUrl);
        } else {
          // Banner / Strip en Apple Wallet es ~3:1 (750x250 o 1125x432 -> 2.6:1 a 3.2:1)
          const isGoodStrip = ratio >= 2.3 && ratio <= 3.5;
          setStripValidation({
            valid: isGoodStrip,
            width: w,
            height: h,
            ratio,
            severity: isGoodStrip ? "ok" : "warn",
            message: isGoodStrip
              ? `✅ Proporción panorámica óptima (${w}×${h}px · relación ${ratio}:1)`
              : `⚠️ Relación ${ratio}:1 (${w}×${h}px). Se recomienda ~3:1 (ej. 1125×432px o 750×250px) para evitar recortes.`,
          });
          setConfig((prev) => ({
            ...prev,
            stripDataUrl: dataUrl,
            useCustomStripBg: true,
          }));
        }
      };
      img.src = dataUrl;
    };
    reader.readAsDataURL(file);
  };

  const onDropFile = (
    e: DragEvent<HTMLDivElement>,
    target: "logo" | "strip"
  ) => {
    e.preventDefault();
    e.stopPropagation();
    const file = e.dataTransfer?.files?.[0];
    if (file) handleImageFile(file, target);
  };

  const onSelectFile = (
    e: ChangeEvent<HTMLInputElement>,
    target: "logo" | "strip"
  ) => {
    const file = e.target.files?.[0];
    if (file) handleImageFile(file, target);
  };

  const detectCurrentGps = () => {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setConfig((prev) => ({
          ...prev,
          lat: Number(pos.coords.latitude.toFixed(6)),
          lng: Number(pos.coords.longitude.toFixed(6)),
        }));
      },
      () => {},
      { enableHighAccuracy: true }
    );
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);
    setSaveFeedback(null);
    try {
      const response = await fetch(endpointUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(config),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) {
        throw new Error(result.error || "Error al guardar la campaña");
      }
      setSaveFeedback({
        type: "success",
        text:
          result.message ||
          "✅ Campaña guardada y sincronizada en Apple Wallet y Google Wallet.",
      });
      if (onSaved) onSaved(config);
    } catch (err: any) {
      setSaveFeedback({
        type: "error",
        text: err?.message || "No se pudo guardar la configuración.",
      });
    } finally {
      setIsSaving(false);
    }
  };

  const goal = Math.max(4, Math.min(12, Number(config.stampGoal) || 10));
  const colsPerRow = Math.ceil(goal / 2);
  const isGoalReached = previewStamps >= goal;

  return (
    <form onSubmit={handleSubmit} className="campaign-admin-grid">
      {/* COLUMNA IZQUIERDA: FORMULARIO EN 3 PESTAÑAS */}
      <div className="campaign-editor-col">
        <div className="campaign-subtabs">
          <button
            type="button"
            className={`c-subtab-btn ${activeTab === "brand" ? "active" : ""}`}
            onClick={() => setActiveTab("brand")}
          >
            🎨 1. Marca y Diseño
          </button>
          <button
            type="button"
            className={`c-subtab-btn ${activeTab === "rules" ? "active" : ""}`}
            onClick={() => setActiveTab("rules")}
          >
            🏆 2. Reglas del Programa
          </button>
          <button
            type="button"
            className={`c-subtab-btn ${activeTab === "location" ? "active" : ""}`}
            onClick={() => setActiveTab("location")}
          >
            📍 3. Ubicación GPS
          </button>
        </div>

        {/* PESTAÑA 1: MARCA Y DISEÑO */}
        {activeTab === "brand" && (
          <div className="c-tab-pane">
            <div className="c-field-block">
              <label className="c-label">Nombre de la Cafetería / Negocio</label>
              <input
                type="text"
                className="input-dark"
                value={config.shopName}
                maxLength={40}
                onChange={(e) => updateField("shopName", e.target.value)}
                placeholder="Ej. Maishas Café"
                required
              />
            </div>

            <div className="c-colors-row">
              <div className="c-color-card">
                <label className="c-label">Color de Fondo</label>
                <div className="c-color-input-wrap">
                  <input
                    type="color"
                    value={config.backgroundColor}
                    onChange={(e) =>
                      updateField("backgroundColor", e.target.value)
                    }
                  />
                  <input
                    type="text"
                    className="input-dark"
                    value={config.backgroundColor}
                    onChange={(e) =>
                      updateField("backgroundColor", e.target.value)
                    }
                  />
                </div>
              </div>

              <div className="c-color-card">
                <label className="c-label">Color de Texto</label>
                <div className="c-color-input-wrap">
                  <input
                    type="color"
                    value={config.foregroundColor}
                    onChange={(e) =>
                      updateField("foregroundColor", e.target.value)
                    }
                  />
                  <input
                    type="text"
                    className="input-dark"
                    value={config.foregroundColor}
                    onChange={(e) =>
                      updateField("foregroundColor", e.target.value)
                    }
                  />
                </div>
              </div>

              <div className="c-color-card">
                <label className="c-label">Color Acento / Sellos</label>
                <div className="c-color-input-wrap">
                  <input
                    type="color"
                    value={config.labelColor}
                    onChange={(e) => updateField("labelColor", e.target.value)}
                  />
                  <input
                    type="text"
                    className="input-dark"
                    value={config.labelColor}
                    onChange={(e) => updateField("labelColor", e.target.value)}
                  />
                </div>
              </div>
            </div>

            {/* Dropzone Logo */}
            <div className="c-field-block">
              <label className="c-label">
                Logo del Comercio (Recomendado: 660×660 px · Relación 1:1)
              </label>
              <div
                className="c-dropzone"
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => onDropFile(e, "logo")}
              >
                <input
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  onChange={(e) => onSelectFile(e, "logo")}
                />
                <div className="c-dropzone-content">
                  {config.logoDataUrl && (
                    <img
                      src={config.logoDataUrl}
                      alt="Logo Preview"
                      className="c-drop-thumb"
                    />
                  )}
                  <div>
                    <strong>Arrastra tu Logo aquí o haz clic para subir</strong>
                    <div className={`c-val-msg ${logoValidation.severity}`}>
                      {logoValidation.message}
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* Dropzone Banner / Strip */}
            <div className="c-field-block">
              <label className="c-label">
                Banner / Fondo de Tira de Sellos (Proporción ~3:1 · 750×250 o
                1125×432 px)
              </label>
              <div
                className="c-dropzone"
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => onDropFile(e, "strip")}
              >
                <input
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  onChange={(e) => onSelectFile(e, "strip")}
                />
                <div className="c-dropzone-content">
                  <div>
                    <strong>
                      Arrastra un Banner personalizado o haz clic para subir
                    </strong>
                    <div className={`c-val-msg ${stripValidation.severity}`}>
                      {stripValidation.message}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* PESTAÑA 2: REGLAS DEL PROGRAMA */}
        {activeTab === "rules" && (
          <div className="c-tab-pane">
            <div className="c-field-block">
              <label className="c-label">
                Meta de Sellos para Premio ({config.stampGoal} sellos)
              </label>
              <input
                type="number"
                min={4}
                max={12}
                className="input-dark"
                value={config.stampGoal}
                onChange={(e) =>
                  updateField("stampGoal", Number(e.target.value) || 10)
                }
              />
            </div>

            <div className="c-field-block">
              <label className="c-label">Texto del Premio / Recompensa</label>
              <input
                type="text"
                className="input-dark"
                value={config.rewardText}
                maxLength={80}
                onChange={(e) => updateField("rewardText", e.target.value)}
                required
              />
            </div>

            <div className="c-field-block">
              <label className="c-label">Enlace al Menú Digital o WhatsApp</label>
              <input
                type="url"
                className="input-dark"
                value={config.menuUrl}
                onChange={(e) => updateField("menuUrl", e.target.value)}
                placeholder="https://wa.me/526421127306"
              />
            </div>

            <div className="c-field-block">
              <label className="c-label">
                Textos Legales y Términos (Reverso de la Tarjeta)
              </label>
              <textarea
                rows={4}
                className="input-dark"
                value={config.termsText}
                onChange={(e) => updateField("termsText", e.target.value)}
              />
            </div>
          </div>
        )}

        {/* PESTAÑA 3: UBICACIÓN GPS */}
        {activeTab === "location" && (
          <div className="c-tab-pane">
            <div className="c-gps-grid">
              <div>
                <label className="c-label">Latitud GPS</label>
                <input
                  type="number"
                  step="any"
                  className="input-dark"
                  value={config.lat}
                  onChange={(e) =>
                    updateField("lat", parseFloat(e.target.value) || 0)
                  }
                />
              </div>
              <div>
                <label className="c-label">Longitud GPS</label>
                <input
                  type="number"
                  step="any"
                  className="input-dark"
                  value={config.lng}
                  onChange={(e) =>
                    updateField("lng", parseFloat(e.target.value) || 0)
                  }
                />
              </div>
              <button
                type="button"
                className="btn-gold-sm"
                onClick={detectCurrentGps}
              >
                📍 Usar Mi GPS
              </button>
            </div>

            <div className="c-field-block">
              <label className="c-label">
                Mensaje de Notificación por Proximidad (~150m en Pantalla de
                Bloqueo)
              </label>
              <input
                type="text"
                className="input-dark"
                maxLength={160}
                value={config.relevantText}
                onChange={(e) => updateField("relevantText", e.target.value)}
              />
            </div>
          </div>
        )}

        {saveFeedback && (
          <div className={`c-save-banner ${saveFeedback.type}`}>
            {saveFeedback.text}
          </div>
        )}

        <button type="submit" className="btn-cam-launch" disabled={isSaving}>
          {isSaving
            ? "⏳ Guardando y sincronizando pases..."
            : "💾 Guardar y Actualizar Plantilla de Campaña"}
        </button>
      </div>

      {/* COLUMNA DERECHA: PREVIEW EN TIEMPO REAL ESTILO APPLE WALLET */}
      <div className="campaign-preview-col">
        <div className="aw-preview-toolbar">
          <span>📱 Vista Previa en Tiempo Real</span>
          <button
            type="button"
            className="btn-icon-sec"
            onClick={() => setShowBackSide(!showBackSide)}
          >
            {showBackSide ? "🔄 Ver Frente" : "ℹ️ Ver Reverso"}
          </button>
        </div>

        <div
          className="aw-card-simulator"
          style={{
            backgroundColor: config.backgroundColor,
            color: config.foregroundColor,
          }}
        >
          {!showBackSide ? (
            <>
              <div className="aw-card-header">
                <div className="aw-brand-left">
                  <img
                    src={config.logoDataUrl || "/api/campaign/logo.png"}
                    alt="Logo"
                    className="aw-logo-img"
                  />
                  <span
                    className="aw-logo-text"
                    style={{ color: config.foregroundColor }}
                  >
                    {config.shopName.toUpperCase()}
                  </span>
                </div>
                <div className="aw-header-field">
                  <span style={{ color: config.labelColor }}>SELLOS</span>
                  <strong style={{ color: config.foregroundColor }}>
                    {previewStamps} / {goal}
                  </strong>
                </div>
              </div>

              {/* Tira de Sellos / Banner */}
              <div
                className="aw-strip-box"
                style={{
                  backgroundImage:
                    config.useCustomStripBg && config.stripDataUrl
                      ? `linear-gradient(rgba(12,10,9,0.55), rgba(12,10,9,0.72)), url(${config.stripDataUrl})`
                      : undefined,
                  borderColor: config.labelColor,
                }}
              >
                <div
                  className="aw-stamps-grid"
                  style={{
                    gridTemplateColumns: `repeat(${colsPerRow}, 1fr)`,
                  }}
                >
                  {Array.from({ length: goal }).map((_, idx) => {
                    const earned = idx < previewStamps;
                    const isLast = idx === goal - 1;
                    return (
                      <div
                        key={idx}
                        className={`aw-stamp-circle ${earned ? "earned" : ""}`}
                        style={{
                          backgroundColor: earned
                            ? config.labelColor
                            : "rgba(0,0,0,0.45)",
                          borderColor: config.labelColor,
                        }}
                      >
                        {isLast ? "★" : "☕️"}
                      </div>
                    );
                  })}
                </div>
                <div
                  className="aw-strip-banner"
                  style={{ borderColor: config.labelColor }}
                >
                  {isGoalReached
                    ? `🎉 ${config.rewardText.toUpperCase()}`
                    : `Faltan ${goal - previewStamps} sellos · Premio: ${config.rewardText}`}
                </div>
              </div>

              {/* Secondary Fields */}
              <div className="aw-secondary-row">
                <div>
                  <span style={{ color: config.labelColor }}>
                    CLIENTE · SOCIO VIP
                  </span>
                  <strong>Ana Sofía López</strong>
                </div>
                <div style={{ textAlign: "right" }}>
                  <span style={{ color: config.labelColor }}>
                    {isGoalReached
                      ? "PREMIO DISPONIBLE"
                      : `FALTAN ${goal - previewStamps} SELLOS`}
                  </span>
                  <strong>{config.rewardText}</strong>
                </div>
              </div>
            </>
          ) : (
            <div className="aw-back-fields">
              <h4>Reverso de la Tarjeta ({config.shopName})</h4>
              <div className="aw-back-item">
                <span style={{ color: config.labelColor }}>
                  PREMIO DEL PROGRAMA ({goal} SELLOS)
                </span>
                <p>{config.rewardText}</p>
              </div>
              <div className="aw-back-item">
                <span style={{ color: config.labelColor }}>
                  MENÚ DIGITAL Y CONTACTO WHATSAPP
                </span>
                <p>{config.menuUrl}</p>
              </div>
              <div className="aw-back-item">
                <span style={{ color: config.labelColor }}>
                  TÉRMINOS Y CONDICIONES LEGALES
                </span>
                <p style={{ whiteSpace: "pre-line" }}>{config.termsText}</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </form>
  );
};

export default CampaignAdminDashboard;
