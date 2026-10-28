"use strict";

// CSV downloads from /api/export. fetch (not a plain link) so a 422 or 429 shows up as a
// message next to the button instead of replacing the page with a JSON error.

const csvExport = (() => {
  const FALLBACK_FILENAME = "changewatch.csv";

  function filenameFrom(response) {
    const header = response.headers.get("Content-Disposition") ?? "";
    const match = header.match(/filename="([^"]+)"/);
    return match ? match[1] : FALLBACK_FILENAME;
  }

  async function errorMessage(response) {
    if (response.status === 429) {
      const seconds = response.headers.get("Retry-After");
      return seconds
        ? `Previše zahteva. Pokušaj ponovo za ${seconds} s.`
        : "Previše zahteva. Pokušaj ponovo malo kasnije.";
    }
    try {
      const { detail } = await response.json();
      // The route's own 422 carries a detail written for the user.
      if (typeof detail === "string") return detail;
    } catch (error) {
      console.warn(`CSV export failed with HTTP ${response.status} and no JSON body`, error);
    }
    return `Izvoz nije uspeo (HTTP ${response.status}).`;
  }

  function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function showError(element, message) {
    element.textContent = message ?? "";
    element.hidden = message === null;
  }

  // Downloads the CSV at `url`; errors go into `errorElement`. The button is disabled
  // while the request runs, so a double click does not start two exports.
  async function download(url, button, errorElement) {
    showError(errorElement, null);
    button.disabled = true;
    try {
      const response = await fetch(url);
      if (!response.ok) {
        showError(errorElement, await errorMessage(response));
        return;
      }
      saveBlob(await response.blob(), filenameFrom(response));
    } catch (error) {
      console.warn(`CSV export from ${url} failed`, error);
      showError(errorElement, "Server nije dostupan. Pokušaj ponovo.");
    } finally {
      button.disabled = false;
    }
  }

  const alertsButton = document.getElementById("export-alerts");
  const alertsError = document.getElementById("export-alerts-error");
  alertsButton.addEventListener("click", () =>
    download("/api/export/alerts.csv", alertsButton, alertsError),
  );

  return { download };
})();
