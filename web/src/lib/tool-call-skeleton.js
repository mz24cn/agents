/**
 * 根据工具的 parameters（JSON Schema）生成"调用测试"的 args JSON 骨架。
 *
 * 规则：
 * - 列出全部 properties（含可选参数），让用户看到完整形状，不需要的键自行删掉
 * - 默认值按类型：string→""、number/integer→0、boolean→false、array→[]、object→{}（递归）
 * - 存在 enum 时取第一个候选值
 * - schema 缺失/非法时返回 {}
 *
 * 输出为 2 空格缩进的 JSON 字符串，可直接粘贴进参数编辑器。
 */
export function buildCallTestSkeleton(tool) {
  return JSON.stringify(buildObjectSkeleton(tool?.parameters), null, 2)
}

function buildObjectSkeleton(parameters) {
  if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)) return {}
  return buildPropertiesSkeleton(parameters.properties)
}

function buildPropertiesSkeleton(properties) {
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) return {}
  const out = {}
  for (const [name, schema] of Object.entries(properties)) {
    out[name] = defaultValueForSchema(schema)
  }
  return out
}

function defaultValueForSchema(schema) {
  if (schema && typeof schema === 'object' && !Array.isArray(schema)
      && Array.isArray(schema.enum) && schema.enum.length > 0) {
    return schema.enum[0]
  }
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return ''
  switch (schema.type) {
    case 'string': return ''
    case 'number':
    case 'integer': return 0
    case 'boolean': return false
    case 'array': return []
    case 'object': return buildPropertiesSkeleton(schema.properties)
    default:
      // 无 type 的 schema：带 properties 按 object 处理，否则给空串占位
      if (schema.properties && typeof schema.properties === 'object') {
        return buildPropertiesSkeleton(schema.properties)
      }
      return ''
  }
}
