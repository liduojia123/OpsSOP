import { describe, expect, it } from 'vitest'

import { renderSafeInlineMd, renderSafeMd } from '../src/safeMd'

describe('renderSafeInlineMd', () => {
  it('只做行内渲染,不产生段落等块级标签', () => {
    const html = renderSafeInlineMd('关联 **磁盘** 使用 `df -h`')
    expect(html).toContain('<strong>磁盘</strong>')
    expect(html).toContain('<code class="md-inline">df -h</code>')
    expect(html).not.toContain('<p')
    expect(html).not.toContain('<ul')
  })

  it('保持转义与链接白名单', () => {
    expect(renderSafeInlineMd('<b>x</b>')).toBe('&lt;b&gt;x&lt;/b&gt;')
    expect(renderSafeInlineMd('[点我](javascript:alert(1))')).not.toContain('<a ')
  })
})

describe('renderSafeMd', () => {
  it('转义原始 HTML,不产生可执行标签', () => {
    const html = renderSafeMd('<script>alert(1)</script><img src=x onerror=alert(1)>')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;script&gt;')
    expect(html).toContain('&lt;img')
  })

  it('渲染标题、列表与分隔线', () => {
    const html = renderSafeMd('## 排查步骤\n\n- 先看磁盘\n- 再看负载\n\n1. 确认告警\n2. 处理\n\n---')
    expect(html).toContain('<h2 class="md-heading">排查步骤</h2>')
    expect(html).toContain('<ul class="md-list"><li>先看磁盘</li><li>再看负载</li></ul>')
    expect(html).toContain('<ol class="md-list"><li>确认告警</li><li>处理</li></ol>')
    expect(html).toContain('<hr class="md-hr">')
  })

  it('渲染表格与对齐', () => {
    const html = renderSafeMd(
      '| 指标 | 阈值 | 建议 |\n|:---|---:|:---:|\n| 磁盘 | 85% | 清理 |\n| 内存 | 90% | 扩容 |',
    )
    expect(html).toContain('<table class="md-table">')
    expect(html).toContain('<th class="md-align-left">指标</th>')
    expect(html).toContain('<th class="md-align-right">阈值</th>')
    expect(html).toContain('<th class="md-align-center">建议</th>')
    expect(html).toContain('<td class="md-align-left">磁盘</td>')
    expect(html).toContain('<td class="md-align-right">85%</td>')
  })

  it('渲染引用、行内代码与粗体', () => {
    const html = renderSafeMd('> 先确认归属\n\n使用 `df -h` 查看 **磁盘** 使用率')
    expect(html).toContain('<blockquote class="md-quote">先确认归属</blockquote>')
    expect(html).toContain('<code class="md-inline">df -h</code>')
    expect(html).toContain('<strong>磁盘</strong>')
  })

  it('围栏代码块保留原样并转义内容', () => {
    const html = renderSafeMd('```bash\necho "<b>hi</b>"\n```')
    expect(html).toContain('<pre class="md-code"><code>echo &quot;&lt;b&gt;hi&lt;/b&gt;&quot;</code></pre>')
  })

  it('未闭合围栏在流式输出中按代码块收尾', () => {
    const html = renderSafeMd('```bash\ndf -h')
    expect(html).toContain('<pre class="md-code"><code>df -h</code></pre>')
  })

  it('链接只允许 http(s)、站内路径与锚点', () => {
    expect(renderSafeMd('[文档](https://example.com/a)')).toContain(
      '<a href="https://example.com/a" target="_blank" rel="noopener noreferrer">文档</a>',
    )
    expect(renderSafeMd('[站内](/faults/1)')).toContain('<a href="/faults/1"')
    // 不安全协议按纯文本保留,不生成链接
    const bad = renderSafeMd('[点我](javascript:alert(1))')
    expect(bad).not.toContain('<a ')
    expect(bad).toContain('点我')
  })

  it('流式中间态(表格分隔行未到齐)不丢失文本', () => {
    const partial = renderSafeMd('| 指标 | 阈值 |\n| 磁盘 |')
    expect(partial).toContain('指标')
    expect(partial).toContain('磁盘')
  })
})