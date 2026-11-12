export const WORKSPACE_EXTENSIONS = ['pdf', 'doc', 'docx', 'txt', 'xlsx', 'csv'];
export const WORKSPACE_ACCEPT = WORKSPACE_EXTENSIONS.map(extension => `.${extension}`).join(',');
export const WORKSPACE_FORMATS_LABEL = WORKSPACE_EXTENSIONS.map(extension => `.${extension}`).join(', ');

export function isWorkspaceFilenameSupported(filename) {
  const basename = String(filename || '').split(/[/\\]/).pop();
  if (!basename || basename.startsWith('~$') || !basename.includes('.')) return false;
  return WORKSPACE_EXTENSIONS.includes(basename.split('.').pop().toLowerCase());
}
