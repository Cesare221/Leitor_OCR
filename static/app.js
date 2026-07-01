(() => {
  function byId(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function initDashboard() {
    const root = byId("dashboardApp");
    const uploadForm = byId("uploadForm");
    const btnProcess = byId("btnProcess");
    const fileInput = byId("fileInput");
    const fileName = byId("fileName");
    const jobsTableBody = byId("jobsTableBody");
    const liveAlert = byId("liveAlert");
    const metricTotal = byId("metricTotal");
    const metricProcessing = byId("metricProcessing");
    const metricCompleted = byId("metricCompleted");
    const metricFailed = byId("metricFailed");
    const historyNote = byId("historyNote");
    const queueHeadline = byId("queueHeadline");
    const insightPrimary = byId("insightPrimary");
    const insightRows = byId("insightRows");
    const processUrl = root ? root.dataset.processUrl : "";
    const feedUrl = root ? root.dataset.feedUrl : "";

    if (!root || !uploadForm || !btnProcess || !fileInput || !fileName || !jobsTableBody) {
      return;
    }

    let submitting = false;
    let pollHandle = null;

    function showAlert(message, kind) {
      if (!liveAlert) {
        return;
      }
      liveAlert.hidden = !message;
      liveAlert.className = `alert ${kind || "info"} dashboard-inline-alert`;
      liveAlert.textContent = message || "";
    }

    function setButtonBusy(isBusy, label) {
      btnProcess.disabled = !!isBusy;
      btnProcess.innerHTML = isBusy
        ? '<span class="spinner"></span>' + (label || "Enviando...")
        : (label || "Enviar para processamento");
    }

    function updateMetrics(summary) {
      if (!summary) {
        return;
      }
      if (metricTotal) metricTotal.textContent = String(summary.total || 0);
      if (metricProcessing) metricProcessing.textContent = String(summary.processing || 0);
      if (metricCompleted) metricCompleted.textContent = String(summary.completed || 0);
      if (metricFailed) metricFailed.textContent = String(summary.failed || 0);
      if (insightPrimary) insightPrimary.textContent = `${summary.processing || 0} job(s)`;
      if (insightRows) insightRows.textContent = String(summary.rows_total || 0);
      const note = summary.processing
        ? "Ha processamentos em andamento. A lista abaixo atualiza automaticamente."
        : "Nenhum processamento em andamento no momento.";
      if (historyNote) historyNote.textContent = note;
      if (queueHeadline) queueHeadline.textContent = note;
    }

    async function refreshFeed() {
      if (!feedUrl) {
        return;
      }
      try {
        const response = await fetch(feedUrl, {
          headers: { Accept: "application/json" },
          cache: "no-store",
          credentials: "same-origin",
        });
        if (!response.ok) {
          return;
        }
        const payload = await response.json();
        if (payload.table_html) {
          jobsTableBody.innerHTML = payload.table_html;
        } else if (Array.isArray(payload.jobs)) {
          const csrfToken = payload.csrf_token || root.dataset.csrf || "";
          jobsTableBody.innerHTML = renderRows(payload.jobs, csrfToken);
        }
        if (payload.summary) {
          updateMetrics(payload.summary);
        }
      } catch (_error) {
        // Mantem a ultima versao do dashboard visivel em caso de falha de rede.
      }
    }

    function renderRows(jobs, csrfToken) {
      if (!jobs || !jobs.length) {
        return '<tr><td colspan="6" class="empty">Nenhum processamento ainda.</td></tr>';
      }
      return jobs.map((job) => {
        const status = String(job.status || "");
        const statusClass = status === "concluido"
          ? "success"
          : status === "erro"
            ? "danger"
            : status === "processando"
              ? "warning"
              : "neutral";
        const download = status === "concluido" && job.output_file
          ? `<a class="button small" href="/download?id=${encodeURIComponent(job.id)}">Baixar</a>`
          : status === "processando"
            ? '<span class="muted">Em andamento</span>'
            : "";
        const error = job.error ? `<tr><td colspan="6"><span class="muted">${escapeHtml(job.error)}</span></td></tr>` : "";
        return `
          <tr>
            <td>
              <div class="job-name">${escapeHtml(job.original_name || "")}</div>
              <div class="job-meta">ID ${escapeHtml(job.id || "")} · ${escapeHtml(String(job.created_at || "").slice(0, 19).replace("T", " "))}</div>
            </td>
            <td><span class="pill ${statusClass}">${escapeHtml(status)}</span></td>
            <td>${escapeHtml(job.rows_count || 0)}</td>
            <td>${escapeHtml(String(job.output_format || "").toUpperCase())}</td>
            <td>${escapeHtml(String(job.retention_until || "").slice(0, 10))}</td>
            <td class="actions">
              ${download}
              <form method="post" action="/delete-job">
                <input type="hidden" name="csrf" value="${escapeHtml(csrfToken)}">
                <input type="hidden" name="job_id" value="${escapeHtml(job.id || "")}">
                <button class="ghost danger small" type="submit">Excluir</button>
              </form>
            </td>
          </tr>
          ${error}
        `;
      }).join("");
    }

    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) {
        fileName.textContent = "PDF, JPG ou PNG ate 25 MB";
        return;
      }
      fileName.textContent = `${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)`;
    });

    uploadForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submitting) {
        return;
      }
      if (!fileInput.files || !fileInput.files[0]) {
        showAlert("Selecione um arquivo antes de enviar.", "error");
        return;
      }

      submitting = true;
      setButtonBusy(true, "Enviando...");
      showAlert("Criando job e enviando arquivo para a fila.", "info");

      try {
        const response = await fetch(processUrl || uploadForm.action, {
          method: "POST",
          body: new FormData(uploadForm),
          headers: {
            Accept: "application/json",
            "X-Requested-With": "fetch",
          },
          credentials: "same-origin",
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.error) {
          throw new Error(payload.error || "Nao foi possivel iniciar o processamento.");
        }

        showAlert(payload.message || "Processamento iniciado com sucesso.", "success");
        uploadForm.reset();
        fileName.textContent = "PDF, JPG ou PNG ate 25 MB";
        await refreshFeed();
      } catch (error) {
        showAlert(error instanceof Error ? error.message : "Falha inesperada ao iniciar o processamento.", "error");
      } finally {
        submitting = false;
        setButtonBusy(false, "Enviar para processamento");
      }
    });

    refreshFeed();
    pollHandle = window.setInterval(refreshFeed, 4000);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        return;
      }
      refreshFeed();
    });
    window.addEventListener("beforeunload", () => {
      if (pollHandle) {
        window.clearInterval(pollHandle);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initDashboard);
  } else {
    initDashboard();
  }
})();
