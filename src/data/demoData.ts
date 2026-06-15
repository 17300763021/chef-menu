import type { Chef, Recipe, RecipeCategory } from '../domain/types'

export const demoChefs: Chef[] = [
  {
    id: 'chen',
    name: '陈大厨',
    slug: 'chen-chef',
    avatarUrl: `${import.meta.env.BASE_URL}avatars/chen-chef.png`,
    theme: 'yellow',
    bio: '永州胃，上海灶。程序写得稳，辣椒放得狠。',
    specialties: ['湘菜', '永州菜', '快手家常菜'],
  },
  {
    id: 'jin',
    name: '金大厨',
    slug: 'jin-chef',
    avatarUrl: `${import.meta.env.BASE_URL}avatars/jin-chef.png`,
    theme: 'pink',
    bio: '认真生活，也认真把每一顿饭做得好看又好吃。',
    specialties: ['清爽家常菜', '精致小炒', '周末料理'],
  },
]

type RecipeSeed = {
  name: string
  category: RecipeCategory
  chefId: string
  emoji: string
  minutes: number
  spicy: number
  difficulty: number
  ingredients: string[]
  steps: string[]
  keywords: string[]
}

const seeds: RecipeSeed[] = [
  { name: '辣椒炒肉', category: '猪肉', chefId: 'chen', emoji: '🌶️', minutes: 20, spicy: 2, difficulty: 1, ingredients: ['五花肉', '青椒', '蒜'], steps: ['五花肉切薄片，小火煸出油脂。', '加入蒜片和青椒，大火翻炒。', '加生抽和盐，炒匀出锅。'], keywords: ['下饭', '湘菜', '工作日'] },
  { name: '糖醋排骨', category: '猪肉', chefId: 'jin', emoji: '🍖', minutes: 55, spicy: 0, difficulty: 3, ingredients: ['排骨', '冰糖', '香醋'], steps: ['排骨焯水后擦干。', '炒糖色，放入排骨翻匀。', '加水焖煮，最后加醋收汁。'], keywords: ['酸甜', '周末硬菜'] },
  { name: '小炒黄牛肉', category: '牛羊肉', chefId: 'chen', emoji: '🥩', minutes: 25, spicy: 3, difficulty: 2, ingredients: ['牛里脊', '小米椒', '香菜'], steps: ['牛肉逆纹切片并腌制。', '热锅快炒牛肉至变色。', '加入辣椒和香菜快速翻匀。'], keywords: ['湘菜', '香辣', '下饭'] },
  { name: '番茄炖牛腩', category: '牛羊肉', chefId: 'jin', emoji: '🍅', minutes: 90, spicy: 0, difficulty: 3, ingredients: ['牛腩', '番茄', '土豆'], steps: ['牛腩焯水。', '番茄炒出汁，加入牛腩和热水。', '小火炖软后加入土豆。'], keywords: ['炖菜', '周末', '暖胃'] },
  { name: '永州血鸭', category: '鸡鸭', chefId: 'chen', emoji: '🦆', minutes: 60, spicy: 3, difficulty: 3, ingredients: ['鸭肉', '鸭血', '青红椒'], steps: ['鸭肉煸炒至出油。', '加入姜蒜和辣椒焖熟。', '淋入鸭血快速翻匀收汁。'], keywords: ['永州菜', '家乡味', '硬菜'] },
  { name: '可乐鸡翅', category: '鸡鸭', chefId: 'jin', emoji: '🍗', minutes: 35, spicy: 0, difficulty: 1, ingredients: ['鸡翅', '可乐', '生抽'], steps: ['鸡翅两面煎香。', '加入可乐和生抽。', '中火焖煮后收汁。'], keywords: ['家常', '儿童友好', '工作日'] },
  { name: '剁椒鱼头', category: '鱼虾海鲜', chefId: 'chen', emoji: '🐟', minutes: 55, spicy: 3, difficulty: 3, ingredients: ['鱼头', '剁椒', '姜葱'], steps: ['鱼头抹盐和料酒腌制。', '铺满剁椒，大火蒸熟。', '撒葱花并淋热油。'], keywords: ['湘菜', '宴客', '周末硬菜'] },
  { name: '蒜蓉粉丝虾', category: '鱼虾海鲜', chefId: 'jin', emoji: '🦐', minutes: 30, spicy: 0, difficulty: 2, ingredients: ['鲜虾', '粉丝', '蒜蓉'], steps: ['粉丝泡软铺盘。', '虾开背摆放，铺上蒜蓉。', '蒸熟后淋蒸鱼豉油。'], keywords: ['鲜香', '好看', '宴客'] },
  { name: '番茄炒蛋', category: '蛋类', chefId: 'chen', emoji: '🍳', minutes: 12, spicy: 0, difficulty: 1, ingredients: ['鸡蛋', '番茄', '葱'], steps: ['鸡蛋炒至蓬松后盛出。', '番茄炒软出汁。', '倒回鸡蛋调味。'], keywords: ['快手', '家常', '工作日'] },
  { name: '虾仁蒸蛋', category: '蛋类', chefId: 'jin', emoji: '🥚', minutes: 22, spicy: 0, difficulty: 2, ingredients: ['鸡蛋', '虾仁', '温水'], steps: ['蛋液和温水按比例混合。', '过滤后盖盘蒸制。', '放入虾仁继续蒸熟。'], keywords: ['清淡', '嫩滑'] },
  { name: '麻婆豆腐', category: '豆制品', chefId: 'chen', emoji: '🥘', minutes: 18, spicy: 3, difficulty: 2, ingredients: ['嫩豆腐', '肉末', '豆瓣酱'], steps: ['豆腐切块焯盐水。', '炒香肉末和豆瓣酱。', '加豆腐烧入味并勾芡。'], keywords: ['川味', '下饭', '快手'] },
  { name: '香煎豆腐', category: '豆制品', chefId: 'jin', emoji: '🧈', minutes: 20, spicy: 1, difficulty: 1, ingredients: ['老豆腐', '葱', '生抽'], steps: ['豆腐切块擦干。', '两面煎至金黄。', '淋入调味汁烧一分钟。'], keywords: ['家常', '素菜'] },
  { name: '蒜蓉上海青', category: '蔬菜菌菇', chefId: 'chen', emoji: '🥬', minutes: 8, spicy: 0, difficulty: 1, ingredients: ['上海青', '蒜'], steps: ['上海青洗净沥干。', '热锅爆香蒜末。', '大火快炒并调味。'], keywords: ['上海', '快手', '清爽'] },
  { name: '口蘑炒芦笋', category: '蔬菜菌菇', chefId: 'jin', emoji: '🍄', minutes: 15, spicy: 0, difficulty: 1, ingredients: ['口蘑', '芦笋', '黑胡椒'], steps: ['口蘑切片煎香。', '加入芦笋翻炒。', '用盐和黑胡椒调味。'], keywords: ['轻食', '清爽', '工作日'] },
  { name: '湖南炒米粉', category: '主食', chefId: 'chen', emoji: '🍜', minutes: 18, spicy: 2, difficulty: 1, ingredients: ['米粉', '鸡蛋', '辣椒'], steps: ['米粉提前泡软。', '炒香鸡蛋和配菜。', '加入米粉快速翻炒调味。'], keywords: ['湖南', '一人食', '夜宵'] },
  { name: '腊肠煲仔饭', category: '主食', chefId: 'jin', emoji: '🍚', minutes: 50, spicy: 0, difficulty: 3, ingredients: ['大米', '腊肠', '青菜'], steps: ['砂锅煮米至水分快干。', '铺腊肠继续焖熟。', '沿锅边淋油形成锅巴。'], keywords: ['周末', '锅巴', '一锅饭'] },
]

export const demoRecipes: Recipe[] = seeds.map((seed, index) => ({
  id: `recipe-${index + 1}`,
  chefId: seed.chefId,
  name: seed.name,
  aliases: [],
  category: seed.category,
  coverUrl: seed.emoji,
  ingredients: seed.ingredients.map((name) => ({ name, amount: '适量' })),
  steps: seed.steps,
  keywords: seed.keywords,
  spicyLevel: seed.spicy,
  difficulty: seed.difficulty,
  minutes: seed.minutes,
  tutorialPlatform: '陈大厨菜单',
  tutorialAuthor: seed.chefId === 'chen' ? '陈大厨' : '金大厨',
  tutorialUrl: '',
  tutorialNote: '这份教程保存后固定展示，不会因刷新或重新推荐而变化。',
  published: true,
}))
