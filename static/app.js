// Confirm dialogs for destructive forms (CSP forbids inline handlers).
document.addEventListener('submit', function (e) {
  var msg = e.target.getAttribute('data-confirm');
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// Select a Markdown snippet on click so it is easy to copy.
document.addEventListener('click', function (e) {
  if (e.target.classList && e.target.classList.contains('snippet')) e.target.select();
});

// Post editor: "Insert image..." uploads the file and inserts Markdown at the cursor.
document.addEventListener('DOMContentLoaded', function () {
  var input = document.getElementById('image-upload');
  var body = document.getElementById('body');
  var status = document.getElementById('upload-status');
  if (!input || !body) return;

  input.addEventListener('change', function () {
    var file = input.files && input.files[0];
    if (!file) return;
    var form = new FormData();
    form.append('image', file);
    form.append('_csrf', input.getAttribute('data-csrf'));
    status.textContent = 'Uploading…';
    fetch(input.getAttribute('data-url'), { method: 'POST', body: form, credentials: 'same-origin' })
      .then(function (r) {
        return r.json().then(function (j) { return { ok: r.ok, json: j }; },
                             function () { return { ok: false, json: { error: 'Upload failed (' + r.status + ').' } }; });
      })
      .then(function (res) {
        if (!res.ok) { status.textContent = res.json.error || 'Upload failed.'; return; }
        var snippet = res.json.markdown;
        var start = body.selectionStart, end = body.selectionEnd, v = body.value;
        var before = v.slice(0, start), after = v.slice(end);
        var pre = before && !before.endsWith('\n') ? '\n\n' : '';
        var post = after && !after.startsWith('\n') ? '\n\n' : '';
        body.value = before + pre + snippet + post + after;
        var pos = (before + pre + snippet).length;
        body.focus(); body.setSelectionRange(pos, pos);
        status.textContent = 'Image inserted.';
      })
      .catch(function () { status.textContent = 'Upload failed. Check your connection and try again.'; })
      .then(function () { input.value = ''; });
  });
});
