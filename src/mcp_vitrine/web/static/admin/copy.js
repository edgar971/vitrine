// The dashboard's one script: a Copy button for a link shown once. Without it, the field is
// still there to select by hand, so the button stays hidden until this runs.
for (const button of document.querySelectorAll("button[data-copy]")) {
  const field = document.getElementById(button.dataset.copy);
  if (!field || !navigator.clipboard) continue;
  button.hidden = false;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(field.value);
      button.textContent = "Copied";
    } catch {
      field.select();
    }
  });
}
