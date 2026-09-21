// Explicit editing of app-owned originals; historical snapshots remain read-only.
let editingMaterial = null;
let materialSaving = false;
let materialLoading = false;
const materialDialog = $("#material-editor-dialog");
const materialText = $("#material-editor-text");
const materialStatus = $("#material-editor-status");
const materialDescriptions = {
  profile: "个人背景：记录你的基础、经历与个人偏好，供老师理解你的学习背景。",
  teaching: "教学约定：保留完整的教学原则和具体条件，可直接修改希望老师怎样讲解、提问和反馈。",
  goals: "学习目标：记录你想学什么、方向和资源约束，不代表这些目标已经达成。",
};
function materialDirty() { return editingMaterial && materialText.value !== editingMaterial.content; }
function materialControls() {
  materialText.disabled = materialSaving || materialLoading || !editingMaterial;
  $("#material-editor-save").disabled = materialText.disabled || !materialText.value.trim() || !materialDirty();
  $("#material-editor-reload").disabled = materialSaving || materialLoading;
  $("#material-editor-close").disabled = materialSaving || materialLoading;
}
async function openMaterialEditor(id) {
  if (materialSaving || materialLoading) return;
  if (materialDirty() && !confirm("放弃尚未保存的修改并重新读取？请先复制需要保留的内容。")) return;
  materialLoading = true;
  editingMaterial = null;
  materialText.value = "";
  materialDialog.dataset.materialId = id;
  $("#material-editor-title").textContent = "编辑学习材料";
  $("#material-editor-description").textContent = materialDescriptions[id];
  materialStatus.textContent = "正在读取……";
  if (!materialDialog.open) materialDialog.showModal();
  materialControls();
  try {
    editingMaterial = await (await api(`/api/materials/${id}`)).json();
    // Textarea normalizes line endings; use the same representation for dirty tracking.
    materialText.value = editingMaterial.content;
    editingMaterial.content = materialText.value;
    $("#material-editor-title").textContent = `编辑${editingMaterial.title}`;
    materialStatus.textContent = "修改后点击保存。";
  } catch (error) { materialStatus.textContent = error.message; }
  finally { materialLoading = false; materialControls(); }
}
function closeMaterialEditor() {
  if (materialSaving || materialLoading) return;
  if (materialDirty() && !confirm("放弃尚未保存的修改？请先复制需要保留的内容。")) return;
  editingMaterial = null;
  materialDialog.close();
}
$("#material-editor-close").addEventListener("click", closeMaterialEditor);
materialDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeMaterialEditor(); });
materialText.addEventListener("input", materialControls);
$("#material-editor-reload").addEventListener("click", () => openMaterialEditor(materialDialog.dataset.materialId));
$("#material-editor-save").addEventListener("click", async () => {
  if ($("#material-editor-save").disabled) return;
  materialSaving = true;
  materialControls();
  materialStatus.textContent = "正在保存……";
  try {
    const saved = await (await api(`/api/materials/${editingMaterial.id}`, {
      revision: editingMaterial.revision, content: materialText.value,
    })).json();
    editingMaterial = saved;
    materialText.value = saved.content;
    editingMaterial.content = materialText.value;
    materialStatus.textContent = "已保存。下一次聊天或重试会读取新版，历史快照保持原样。";
    if ($("#teaching-dialog").open) await showTeaching();
    await refreshTeachingNotice();
  } catch (error) { materialStatus.textContent = `${error.message} 编辑内容仍保留，请勿刷新页面。`; }
  finally { materialSaving = false; materialControls(); }
});
window.addEventListener("beforeunload", (event) => {
  if (materialDirty() || materialSaving) { event.preventDefault(); event.returnValue = ""; }
});
