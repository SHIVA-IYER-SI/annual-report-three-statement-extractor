const $ = (selector) => document.querySelector(selector);
let currentJobId = null;
let currentPreview = null;
let currentArtifactId = null;
let activeSheet = "INCOME STATEMENT";
let pollTimer = null;
let blueprintData = null;
const MIN_FILES = 1;
const MAX_FILES = 10;
const MAX_FILE_BYTES = 250 * 1024 * 1024;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function bytes(value) {
  if (!value && value !== 0) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${Math.ceil(value / 1024)} KB`;
  return `${(value / 1024 ** 2).toFixed(1)} MB`;
}

function setBadge(element, status, label) {
  element.className = "statusBadge";
  if (["READY", "PASS"].includes(status)) element.classList.add("available");
  else if (["REVIEW_REQUIRED", "REVIEW", "NOT_RUN"].includes(status)) element.classList.add("review");
  else if (["FAILED", "FAIL", "ERROR"].includes(status)) element.classList.add("error");
  else element.classList.add("notReady");
  element.textContent = label || String(status || "NOT_READY").replaceAll("_", " ");
}

async function loadBlueprint() {
  try {
    const response = await fetch("/api/blueprint");
    blueprintData = await response.json();
    const lineTotal = Object.values(blueprintData.line_item_counts).reduce((a, b) => a + b, 0);
    const checkTotal = Object.values(blueprintData.validation_checks).reduce((a, b) => a + b.length, 0);
    $("#lineItemTotal").textContent = lineTotal;
    $("#checkTotal").textContent = checkTotal;
    renderBlueprintChecks();
  } catch (error) {
    console.error("Blueprint metadata could not be loaded", error);
  }
}

function renderBlueprintChecks() {
  if (!blueprintData) return;
  $("#blueprintChecks").innerHTML = Object.entries(blueprintData.validation_checks).map(([statement, checks]) => `
    <section class="blueprintGroup">
      <h3>${escapeHtml(statement)} · ${checks.length}</h3>
      <ul>${checks.map((item) => `<li><strong>${escapeHtml(item.id)}</strong> — ${escapeHtml(item.name)}</li>`).join("")}</ul>
    </section>
  `).join("");
}

function validateFiles(files) {
  if (files.length < MIN_FILES || files.length > MAX_FILES) {
    return `Select between ${MIN_FILES} and ${MAX_FILES} files.`;
  }
  const oversized = files.find((file) => file.size > MAX_FILE_BYTES);
  if (oversized) return `${oversized.name} exceeds the 250 MB per-file limit.`;
  const unsupported = files.find((file) => !/\.(pdf|xml|xhtml|html|htm)$/i.test(file.name));
  if (unsupported) return `${unsupported.name} is not a supported PDF or XBRL/iXBRL file.`;
  return null;
}

function renderSelectedFiles(files) {
  const total = files.reduce((sum, file) => sum + file.size, 0);
  const header = files.length ? `<div class="fileChip fileSummary"><strong>${files.length} file(s) selected</strong><span>${bytes(total)} total</span></div>` : "";
  $("#fileList").innerHTML = header + files.map((file, index) => `<div class="fileChip"><span>${index + 1}. ${escapeHtml(file.name)}</span><span>${bytes(file.size)}</span></div>`).join("");
}

$("#files").addEventListener("change", (event) => {
  const files = Array.from(event.target.files || []);
  renderSelectedFiles(files);
  const error = validateFiles(files);
  if (error) showError(error);
  else $("#errorBox").classList.add("hidden");
});

async function responseData(response) {
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}

$("#extractionForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  clearInterval(pollTimer);
  const files = Array.from($("#files").files || []);
  const fileError = validateFiles(files);
  if (fileError) {
    showError(fileError);
    return;
  }
  const button = $("#submitButton");
  button.disabled = true;
  button.textContent = `Uploading 0 / ${files.length}`;
  $("#resultSection").classList.add("hidden");
  $("#statusSection").classList.remove("hidden");
  $("#errorBox").classList.add("hidden");
  setBadge($("#statusBadge"), "UPLOADING", "Uploading");
  $("#statusTitle").textContent = "Uploading source files";
  $("#statusMessage").textContent = `Uploading 0 of ${files.length} files to temporary storage.`;
  $("#progressBar").style.width = "5%";
  $("#progressText").textContent = "5%";

  try {
    const initForm = new FormData();
    initForm.append("scope", $("#scope").value);
    initForm.append("fallback_unit", $("#fallbackUnit").value);
    initForm.append("file_count", String(files.length));
    const initResponse = await fetch("/api/jobs/init", { method: "POST", body: initForm });
    const initialized = await responseData(initResponse);
    currentJobId = initialized.id;
    localStorage.setItem("annualReportExtractorJob", currentJobId);
    renderStatus(initialized);

    for (let index = 0; index < files.length; index += 1) {
      const uploadForm = new FormData();
      uploadForm.append("file", files[index]);
      button.textContent = `Uploading ${index + 1} / ${files.length}`;
      $("#statusMessage").textContent = `Uploading ${files[index].name} (${index + 1} of ${files.length})`;
      const uploadResponse = await fetch(`/api/jobs/${currentJobId}/files`, { method: "POST", body: uploadForm });
      const uploaded = await responseData(uploadResponse);
      renderStatus(uploaded);
    }

    button.textContent = "Detecting companies…";
    const startResponse = await fetch(`/api/jobs/${currentJobId}/start`, { method: "POST" });
    const started = await responseData(startResponse);
    renderStatus(started);
    pollTimer = setInterval(pollJob, 1500);
  } catch (error) {
    if (currentJobId) {
      try { await fetch(`/api/jobs/${currentJobId}`, { method: "DELETE" }); } catch (_) { /* best effort */ }
      localStorage.removeItem("annualReportExtractorJob");
      currentJobId = null;
    }
    showError(error.message || "Upload failed");
  } finally {
    button.disabled = false;
    button.textContent = "Analyze annual reports";
  }
});

async function pollJob() {
  if (!currentJobId) return;
  try {
    const response = await fetch(`/api/jobs/${currentJobId}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Job unavailable");
    renderStatus(data);
    if (["READY", "REVIEW_REQUIRED", "FAILED"].includes(data.status)) {
      clearInterval(pollTimer);
      if (data.result) renderBatchResult(data);
      if (data.status === "FAILED" && !data.result) showError(data.error?.message || "Extraction failed");
    }
  } catch (error) {
    clearInterval(pollTimer);
    showError(error.message || "Status check failed");
  }
}

function renderStatus(data) {
  $("#statusSection").classList.remove("hidden");
  const titles = {
    READY: "Company workbooks generated",
    REVIEW_REQUIRED: "Workbooks generated — review required",
    FAILED: "Extraction finished with errors",
  };
  $("#statusTitle").textContent = titles[data.status] || String(data.status || "PROCESSING").replaceAll("_", " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());
  $("#statusMessage").textContent = data.message || "Processing";
  setBadge($("#statusBadge"), data.status, data.status);
  const progress = Number(data.progress || 0);
  $("#progressBar").style.width = `${progress}%`;
  $("#progressText").textContent = `${progress}%`;
  const expiry = data.expires_at ? new Date(data.expires_at) : null;
  $("#expiryText").textContent = expiry ? `Temporary files expire ${expiry.toLocaleString()}` : "";
}

function showError(message) {
  $("#statusSection").classList.remove("hidden");
  $("#statusTitle").textContent = "Check upload requirements";
  $("#statusMessage").textContent = "Correct the issue below before starting the analysis.";
  const box = $("#errorBox");
  box.textContent = message;
  box.classList.remove("hidden");
  setBadge($("#statusBadge"), "FAILED", "Failed");
}

function detectionRows(artifact) {
  return (artifact.detection || []).map((item) => `
    <tr>
      <td>${escapeHtml(item.filename)}</td>
      <td>${escapeHtml(item.detected_company)}</td>
      <td>${Math.round(Number(item.confidence || 0) * 100)}%</td>
      <td>${escapeHtml(item.method)}</td>
      <td>${item.review_required ? "Review" : "Matched"}</td>
    </tr>
  `).join("");
}

function checklistHtml(items) {
  return (items || []).map((item) => {
    const status = String(item.status || "NOT_RUN").toLowerCase();
    const icon = item.status === "PASS" ? "✓" : item.status === "FAIL" ? "!" : "•";
    return `<article class="checkItem"><span class="checkMark ${status}">${icon}</span><div><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.detail)}</small></div></article>`;
  }).join("");
}

function statisticsHtml(stats) {
  const statementRows = stats.statement_rows || {};
  const items = [
    [stats.files, "source files"],
    [stats.raw_rows_detected, "raw table/fact rows"],
    [stats.mapped_source_rows, "mapped source rows"],
    [stats.selected_values, "selected year-values"],
    [statementRows["INCOME STATEMENT"] || 0, "Income Statement rows"],
    [statementRows["BALANCE SHEET"] || 0, "Balance Sheet rows"],
    [statementRows["CASH FLOW STATEMENT"] || 0, "Cash Flow rows"],
    [stats.restatement_comparisons || 0, "comparative conflicts resolved"],
  ];
  return items.map(([value, label]) => `<div class="statCard"><strong>${escapeHtml(value ?? 0)}</strong><span>${escapeHtml(label)}</span></div>`).join("");
}

function warningsHtml(artifact) {
  const warnings = [...(artifact.warnings || [])];
  if (artifact.unmapped_count) warnings.push(`${artifact.unmapped_count} source row(s) were excluded because no confident canonical mapping was available.`);
  (artifact.detection || []).filter((item) => item.review_required).forEach((item) => {
    warnings.push(`Confirm company detection for ${item.filename}: ${item.detected_company} (${Math.round(Number(item.confidence || 0) * 100)}% confidence).`);
  });
  if (!warnings.length) return `<div class="notice good">No additional extraction warnings were recorded.</div>`;
  return warnings.map((warning) => `<div class="notice">${escapeHtml(warning)}</div>`).join("");
}

function unmappedHtml(rows) {
  return (rows || []).map((row) => `<tr><td>${escapeHtml(row.source_file)}</td><td>${escapeHtml(row.page ?? "—")}</td><td>${escapeHtml(row.original_label)}</td><td>${escapeHtml(row.top_candidate ?? "—")}</td><td>${escapeHtml(row.top_score ?? "—")}</td></tr>`).join("");
}

function renderArtifact(artifact) {
  if (artifact.status === "FAILED") {
    return `
      <article class="card artifactGroup failedArtifact">
        <div class="artifactGroupHeader">
          <div><span class="eyebrow">Detected company group</span><h3>${escapeHtml(artifact.company_name)}</h3><p>${escapeHtml((artifact.source_files || []).join(" · "))}</p></div>
          <span class="statusBadge error">Failed</span>
        </div>
        <div class="errorBox">${escapeHtml(artifact.error?.message || "This company group could not be processed.")}</div>
        <details class="detailsCard"><summary>Company detection details</summary><div class="tableWrap"><table class="history"><thead><tr><th>File</th><th>Detected company</th><th>Confidence</th><th>Method</th><th>Status</th></tr></thead><tbody>${detectionRows(artifact)}</tbody></table></div></details>
      </article>
    `;
  }
  const vs = artifact.validation_summary || {};
  const downloadUrl = `/api/jobs/${currentJobId}/download/workbook/${encodeURIComponent(artifact.artifact_id)}`;
  const years = (artifact.years || []).map((year) => `FY${String(year).slice(-2)}`).join(" · ");
  return `
    <article class="card artifactGroup">
      <div class="artifactGroupHeader">
        <div>
          <span class="eyebrow">${(artifact.source_files || []).length} source file(s)</span>
          <h3>${escapeHtml(artifact.company_name)}</h3>
          <p>${escapeHtml(artifact.scope)} · ${escapeHtml(years)}</p>
        </div>
        <span class="statusBadge ${artifact.status === "READY" ? "available" : "review"}">${artifact.status === "READY" ? "Ready" : "Review required"}</span>
      </div>
      <div class="artifactCard compactArtifact">
        <div class="fileIcon" aria-hidden="true">XLSX</div>
        <div class="artifactBody">
          <strong>${escapeHtml(artifact.workbook_filename)}</strong>
          <p>${escapeHtml((artifact.source_files || []).join(" · "))}</p>
          <div class="artifactMeta"><span>${vs.critical_count || 0} critical</span><span>${vs.error_count || 0} errors</span><span>${vs.warning_count || 0} warnings</span><span>${bytes(artifact.workbook_bytes)}</span></div>
        </div>
        <div class="actions">
          <button class="button secondary previewArtifact" type="button" data-artifact-id="${escapeHtml(artifact.artifact_id)}">Open / Preview</button>
          <a class="button primary" href="${downloadUrl}" download="${escapeHtml(artifact.workbook_filename)}">Download Excel</a>
        </div>
      </div>
      <details class="detailsCard artifactDetails">
        <summary>Company grouping, checklist and review details</summary>
        <h4>Detected source grouping</h4>
        <div class="tableWrap"><table class="history"><thead><tr><th>File</th><th>Detected company</th><th>Confidence</th><th>Method</th><th>Status</th></tr></thead><tbody>${detectionRows(artifact)}</tbody></table></div>
        <h4>Validation checklist</h4>
        <div class="checklist">${checklistHtml(artifact.checklist)}</div>
        <div class="twoColumn artifactReviewGrid">
          <div><h4>Extraction summary</h4><div class="statGrid">${statisticsHtml(artifact.statistics || {})}</div></div>
          <div><h4>Warnings and review</h4><div class="noticeList">${warningsHtml(artifact)}</div></div>
        </div>
        ${artifact.unmapped_count ? `<h4>Unmapped source rows (${artifact.unmapped_count})</h4><div class="tableWrap"><table class="history"><thead><tr><th>File</th><th>Page</th><th>Original label</th><th>Top candidate</th><th>Score</th></tr></thead><tbody>${unmappedHtml(artifact.unmapped_preview)}</tbody></table></div>` : ""}
      </details>
    </article>
  `;
}

function renderBatchResult(data) {
  const result = data.result;
  $("#resultSection").classList.remove("hidden");
  $("#resultTitle").textContent = `${result.success_count} workbook(s) generated`;
  $("#resultMeta").textContent = `${result.company_count} detected company group(s) · ${data.files.length} uploaded file(s) · ${result.failed_count} failed group(s)`;
  setBadge($("#resultBadge"), data.status, data.status === "READY" ? "Ready" : data.status === "FAILED" ? "Failed" : "Review required");
  $("#groupingNotice").innerHTML = `<strong>Automatic company differentiator:</strong> matching reports were combined company-wise. Different companies were kept separate. Open each card to verify detection confidence before relying on the workbook.`;
  $("#artifactResults").innerHTML = (result.artifacts || []).map(renderArtifact).join("");

  const allButton = $("#downloadAllButton");
  if (result.zip_filename && result.success_count > 0) {
    allButton.href = `/api/jobs/${currentJobId}/download/all`;
    allButton.download = result.zip_filename;
    allButton.classList.remove("hidden");
  } else {
    allButton.classList.add("hidden");
  }
  window.scrollTo({ top: $("#resultSection").offsetTop - 20, behavior: "smooth" });
}

$("#artifactResults").addEventListener("click", (event) => {
  const button = event.target.closest(".previewArtifact");
  if (!button) return;
  openPreview(button.dataset.artifactId);
});

async function openPreview(artifactId) {
  if (!currentJobId || !artifactId) return;
  currentArtifactId = artifactId;
  $("#drawerBackdrop").classList.remove("hidden");
  $("#drawerTitle").textContent = "Loading workbook preview…";
  $("#previewSummary").textContent = "";
  try {
    const response = await fetch(`/api/jobs/${currentJobId}/preview/${encodeURIComponent(artifactId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Preview unavailable");
    currentPreview = data;
    activeSheet = "INCOME STATEMENT";
    const url = `/api/jobs/${currentJobId}/download/workbook/${encodeURIComponent(artifactId)}`;
    $("#drawerDownload").href = url;
    $("#drawerDownload").download = data.artifact.filename;
    renderPreview();
  } catch (error) {
    $("#drawerTitle").textContent = "Preview unavailable";
    $("#previewSummary").textContent = error.message;
  }
}

function renderPreview() {
  if (!currentPreview) return;
  $("#drawerTitle").textContent = currentPreview.artifact.filename;
  const vs = currentPreview.validation_summary || {};
  $("#previewSummary").innerHTML = `<strong>${escapeHtml(currentPreview.company_name)}</strong><span>${escapeHtml(currentPreview.scope)}</span><span>${currentPreview.years.map(escapeHtml).join(" · ")}</span><span>${vs.critical_count || 0} critical · ${vs.warning_count || 0} warnings</span>`;
  $("#tabs").innerHTML = currentPreview.sheets.map((sheet) => `<button type="button" class="${sheet.name === activeSheet ? "activeTab" : ""}" data-sheet="${escapeHtml(sheet.name)}">${escapeHtml(sheet.name)}</button>`).join("");
  $("#tabs").querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
    activeSheet = button.dataset.sheet;
    renderPreview();
  }));
  const sheet = currentPreview.sheets.find((item) => item.name === activeSheet);
  if (!sheet) return;
  $("#previewHead").innerHTML = `<tr><th>Statement line</th>${sheet.years.map((year) => `<th>${escapeHtml(year)}</th>`).join("")}</tr>`;
  $("#previewBody").innerHTML = sheet.rows.map((row) => {
    const cells = sheet.years.map((year) => {
      const cell = row.cells[year] || {};
      const cls = cell.review_required ? "reviewCell" : cell.missing ? "missingCell" : "";
      const title = cell.formula ? "Formula-derived from disclosed canonical rows" : cell.source_page ? `${cell.source_file || "Source"}, page ${cell.source_page}${cell.original_label ? ` — ${cell.original_label}` : ""}` : "";
      const value = cell.display_value || (cell.missing ? "Blank — not disclosed" : "");
      return `<td class="${cls}" title="${escapeHtml(title)}">${escapeHtml(value)}</td>`;
    }).join("");
    return `<tr class="${escapeHtml(row.row_type)}"><th style="padding-left:${0.75 + Number(row.hierarchy_level || 0) * 1.25}rem">${escapeHtml(row.label)}</th>${cells}</tr>`;
  }).join("");
}

function closeDrawer() {
  $("#drawerBackdrop").classList.add("hidden");
}
$("#closeDrawer").addEventListener("click", closeDrawer);
$("#drawerBackdrop").addEventListener("mousedown", (event) => {
  if (event.currentTarget === event.target) closeDrawer();
});

$("#deleteButton").addEventListener("click", async () => {
  if (!currentJobId) return;
  const confirmed = window.confirm("Delete the uploaded files and generated outputs from the server now?");
  if (!confirmed) return;
  await fetch(`/api/jobs/${currentJobId}`, { method: "DELETE" });
  localStorage.removeItem("annualReportExtractorJob");
  currentJobId = null;
  currentPreview = null;
  currentArtifactId = null;
  $("#resultSection").classList.add("hidden");
  $("#statusSection").classList.add("hidden");
  $("#extractionForm").reset();
  $("#fileList").innerHTML = "";
  window.scrollTo({ top: 0, behavior: "smooth" });
});

async function restoreJob() {
  const saved = localStorage.getItem("annualReportExtractorJob");
  if (!saved) return;
  try {
    const response = await fetch(`/api/jobs/${saved}`);
    if (!response.ok) throw new Error();
    const data = await response.json();
    currentJobId = saved;
    renderStatus(data);
    if (data.result && ["READY", "REVIEW_REQUIRED", "FAILED"].includes(data.status)) renderBatchResult(data);
    else if (data.status !== "FAILED") pollTimer = setInterval(pollJob, 1500);
  } catch {
    localStorage.removeItem("annualReportExtractorJob");
  }
}

loadBlueprint();
restoreJob();
