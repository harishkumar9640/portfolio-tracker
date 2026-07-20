/* tax.js — upload modal logic for the Tax & P&L page.
   Lives at webapp/static/js/tax.js. Served under /static/js/tax.js. */

(function () {
  "use strict";

  // ----- Modal open/close -----
  const modal = document.getElementById("uploadModal");
  const openBtn = document.getElementById("openUploadBtn");
  if (openBtn) {
    openBtn.addEventListener("click", () => {
      modal.style.display = "flex";
      document.getElementById("fileList").innerHTML = "";
      document.getElementById("uploadStatus").style.display = "none";
    });
  }
  window.closeUploadModal = function () {
    modal.style.display = "none";
  };
  modal && modal.addEventListener("click", (e) => {
    if (e.target === modal) closeUploadModal();
  });

  // ----- Drop zone -----
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("fileInput");
  if (dropzone) {
    dropzone.addEventListener("click", () => fileInput.click());
    ["dragenter", "dragover"].forEach((ev) =>
      dropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        dropzone.style.background = "#eef";
        dropzone.style.borderColor = "#88c";
      })
    );
    ["dragleave", "drop"].forEach((ev) =>
      dropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        dropzone.style.background = "#fafafa";
        dropzone.style.borderColor = "#ccc";
      })
    );
    dropzone.addEventListener("drop", (e) => {
      fileInput.files = e.dataTransfer.files;
      renderFileList();
    });
    fileInput.addEventListener("change", renderFileList);
  }

  function renderFileList() {
    const list = document.getElementById("fileList");
    const files = Array.from(fileInput.files || []);
    if (!files.length) {
      list.innerHTML = "";
      return;
    }
    const html = files
      .map((f) => {
        const kb = (f.size / 1024).toFixed(1);
        return `<div>📄 <strong>${escapeHtml(f.name)}</strong> <span class="text-muted">(${kb} KB)</span></div>`;
      })
      .join("");
    const total = files.reduce((a, f) => a + f.size, 0);
    list.innerHTML = html +
      `<div class="mt-1 text-muted">${files.length} file(s), ${(total / 1024).toFixed(1)} KB total</div>`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // ----- Submit -----
  const form = document.getElementById("uploadForm");
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const files = fileInput.files;
      if (!files || !files.length) {
        showStatus("error", "Please select at least one file.");
        return;
      }
      if (files.length > 10) {
        showStatus("error", "Maximum 10 files per session.");
        return;
      }
      for (const f of files) {
        if (f.size > 20 * 1024 * 1024) {
          showStatus("error", `${f.name} is too large (max 20 MB).`);
          return;
        }
      }
      const fd = new FormData(form);
      // fileInput is not in form because it has display:none; add manually
      fd.delete("files");
      for (const f of files) fd.append("files", f);

      const submitBtn = document.getElementById("uploadSubmitBtn");
      submitBtn.disabled = true;
      submitBtn.textContent = "Uploading…";
      showStatus("info", "Uploading & parsing…");

      try {
        const r = await fetch("/api/tax/upload", { method: "POST", body: fd });
        const data = await r.json();
        if (!r.ok) {
          showStatus("error", data.detail?.error || data.detail || "Upload failed");
          if (data.detail?.rejected) {
            showStatus("error",
              "Upload failed: " +
              data.detail.rejected.map((x) => `${x.name}: ${x.error}`).join("; "));
          }
          return;
        }
        // Success — redirect to the session view
        showStatus("success",
          `Uploaded ${data.files_uploaded.length} file(s). Redirecting to analysis…`);
        setTimeout(() => { window.location.href = data.tax_url; }, 600);
      } catch (err) {
        showStatus("error", "Network error: " + err.message);
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Upload & analyze";
      }
    });
  }

  function showStatus(kind, msg) {
    const el = document.getElementById("uploadStatus");
    el.style.display = "block";
    const colors = {
      error:   { bg: "#fee", border: "#c00" },
      success: { bg: "#efe", border: "#0a0" },
      info:    { bg: "#eef", border: "#06c" },
    };
    const c = colors[kind] || colors.info;
    el.style.background = c.bg;
    el.style.borderLeft = `4px solid ${c.border}`;
    el.style.padding = "0.5rem 0.8rem";
    el.style.borderRadius = "4px";
    el.innerHTML = msg;
  }
})();
