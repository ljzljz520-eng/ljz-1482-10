'use strict';
/** 简化认证：x-user-id 头识别用户；项目级 ACL 控制 owner/editor/viewer */

function authMiddleware(store) {
  return (req, res, next) => {
    const uid = req.get('x-user-id');
    if (!uid) return res.status(401).json({ error: 'missing x-user-id' });
    const user = store.get('users', uid);
    if (!user) return res.status(401).json({ error: 'unknown user' });
    req.user = user;
    next();
  };
}

function projectRole(store, projectId, userId) {
  const project = store.get('projects', projectId);
  if (!project) return null;
  if (project.owner_id === userId) return 'owner';
  const acl = store.findOne('acls', (a) => a.project_id === projectId && a.user_id === userId);
  return acl ? acl.role : null;
}

const LEVEL = { viewer: 1, editor: 2, owner: 3 };

/** 每次请求实时校验——权限被收回后，恢复/编辑立即失效 */
function requireProjectRole(store, minRole) {
  return (req, res, next) => {
    const projectId = req.params.projectId || req.params.id;
    const role = projectRole(store, projectId, req.user.id);
    if (!role || LEVEL[role] < LEVEL[minRole]) {
      return res.status(403).json({ error: 'forbidden', need: minRole, have: role || 'none' });
    }
    req.project = store.get('projects', projectId);
    req.projectRole = role;
    next();
  };
}

module.exports = { authMiddleware, requireProjectRole, projectRole };
