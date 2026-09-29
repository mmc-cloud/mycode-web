import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'

// https://vitepress.dev/reference/site-config
export default withMermaid(defineConfig({
  title: 'MyCode Docs',
  description: 'MyCode Documentation',
  lang: 'zh-CN',
  base: '/docs/',

  vite: {
    optimizeDeps: {
      include: [
        'fastdom',
        'fastdom/extensions/fastdom-promised.js'
      ]
    }
  },

  themeConfig: {
    // https://vitepress.dev/reference/default-theme-config
    nav: [
      { text: '文档首页', link: '/' },
      { text: '项目主页', link: 'https://mycode.icu/' },
      { text: 'Web Demo', link: 'https://mycode.icu/web/' },
      { text: 'GitHub', link: 'https://github.com/mmc-cloud/mycode' }
    ],

    // 全站统一 Sidebar，不按目录切换
    sidebar: [
      {
        text: '开始',
        items: [
          { text: '文档首页', link: '/' },
          { text: '快速开始', link: '/00-getting-started/' }
        ]
      },
      {
        text: '项目拆解',
        items: [
          { text: '项目总览', link: '/01-overview/' },
          { text: 'Agent 与 Runtime', link: '/02-agent/' },
          { text: 'Tool 系统', link: '/03-tools/' },
          { text: 'Context / Memory / Session', link: '/04-context/' },
          { text: 'SubAgent ', link: '/05-subagent/' },
          { text: 'MCP/Skill', link: '/06-mcp-skill/' },
          { text: 'Evaluation / Harbor', link: '/07-evaluation/' },
          { text: 'MyCode 演进与设计取舍', link: '/08-evolution/' }
        ]
      }
    ],

    // 右侧当前页面标题目录
    outline: {
      level: [2, 3],
      label: '本页目录'
    },

    docFooter: {
      prev: '上一页',
      next: '下一页'
    },
    returnToTopLabel: '回到顶部',

    search: {
      provider: 'local',
      options: {
        translations: {
          button: {
            buttonText: '搜索',
            buttonAriaLabel: '搜索文档'
          },
          modal: {
            noResultsText: '未找到相关结果',
            resetButtonTitle: '清除查询条件',
            footer: {
              selectText: '选择',
              navigateText: '切换',
              closeText: '关闭'
            }
          }
        }
      }
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/mmc-cloud/mycode' }
    ]
  }
}))
