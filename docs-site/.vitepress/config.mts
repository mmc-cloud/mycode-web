import { defineConfig } from 'vitepress'

// https://vitepress.dev/reference/site-config
export default defineConfig({
  title: 'MyCode Docs',
  description: 'MyCode Documentation',
  lang: 'zh-CN',
  base: '/docs/',

  themeConfig: {
    // https://vitepress.dev/reference/default-theme-config
    nav: [
      { text: '文档首页', link: '/' },
      { text: 'GitHub', link: 'https://github.com/mmc-cloud/mycode' }
    ],

    // 全站统一 Sidebar，不按目录切换
    sidebar: [
      {
        text: '开始使用',
        items: [
          { text: '文档首页', link: '/' },
          { text: '快速开始', link: '/getting-started/' }
        ]
      },
      {
        text: '扩展能力',
        items: [{ text: 'MCP', link: '/mcp/' }]
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
})
