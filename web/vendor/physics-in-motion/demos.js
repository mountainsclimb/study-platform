// 由 physics-in-motion 的 10 个自研教学案例导出，供知序「物理演示」页使用。
// 上游：https://github.com/xuxuezhou/physics-in-motion （MIT，Copyright (c) 2026 xuxuezhou）
// 动画数据不预生成：由 physics-core.js 在前端按 visualization 参数即时计算。
export const DEMOS = [
  {
    "id": "demo-incline",
    "title": "斜面下滑进入水平面",
    "modules": [
      "动力学",
      "功和能"
    ],
    "difficulty": 2,
    "question": "如图,一质量 m=1 kg 的物块从倾角 θ=37° 的斜面顶端由静止释放,斜面长 L=2 m,物块与斜面间动摩擦因数 μ₁=0.25,与水平面间动摩擦因数 μ₂=0.5。物块滑到底端后进入水平面,最终停止。取 g=10 m/s²,sin37°=0.6,cos37°=0.8。求:",
    "subQuestions": [
      {
        "id": "q1",
        "text": "(1) 物块到达斜面底端时的速度大小 v₁;",
        "answer": {
          "value": 4,
          "unit": "m/s",
          "expression": "v_1=\\sqrt{2g(\\sin\\theta-\\mu_1\\cos\\theta)L}",
          "tolerance": 0.02,
          "scoringPoints": [
            "沿斜面方向牛顿第二定律",
            "运动学公式 v²=2aL"
          ]
        }
      },
      {
        "id": "q2",
        "text": "(2) 物块在水平面上滑行的距离 s₂。",
        "answer": {
          "value": 1.6,
          "unit": "m",
          "expression": "s_2=\\dfrac{v_1^2}{2\\mu_2 g}",
          "tolerance": 0.02,
          "scoringPoints": [
            "水平面摩擦减速度 a₂=μ₂g",
            "匀减速到停止"
          ]
        }
      }
    ],
    "answerSummary": "v₁ = 4 m/s;s₂ = 1.6 m。",
    "solution": {
      "topics": [
        "牛顿第二定律",
        "匀变速直线运动",
        "摩擦力",
        "能量守恒"
      ],
      "assumptions": [
        "物块视为质点",
        "斜面与水平面平滑连接,拐角处速度大小不变",
        "忽略空气阻力"
      ],
      "modelDescription": "研究对象为物块;以斜面底端为原点。运动分两个阶段:①沿斜面匀加速下滑;②在水平面匀减速直至停止。",
      "steps": [
        {
          "id": "s1",
          "title": "斜面上受力分析,求加速度",
          "law": "牛顿第二定律",
          "latex": "a_1 = g(\\sin\\theta - \\mu_1\\cos\\theta) = 10\\times(0.6-0.25\\times0.8) = 4\\ \\text{m/s}^2",
          "derivation": "沿斜面方向:mg·sinθ − μ₁mg·cosθ = ma₁,代入得 a₁ = 4 m/s²。",
          "why": "物块只受重力、支持力、滑动摩擦力,沿斜面方向合力恒定,适用牛顿第二定律。",
          "bindPhase": "phase_incline"
        },
        {
          "id": "s2",
          "title": "求底端速度",
          "law": "匀变速直线运动公式",
          "latex": "v_1 = \\sqrt{2a_1L} = \\sqrt{2\\times4\\times2} = 4\\ \\text{m/s}",
          "derivation": "初速为零,由 v² = 2a₁L 得 v₁ = 4 m/s。",
          "why": "斜面上加速度恒定,可用匀变速运动公式。",
          "bindEvent": "enter_horizontal"
        },
        {
          "id": "s3",
          "title": "水平面减速",
          "law": "牛顿第二定律 + 匀变速公式",
          "latex": "s_2 = \\dfrac{v_1^2}{2\\mu_2 g} = \\dfrac{16}{2\\times0.5\\times10} = 1.6\\ \\text{m}",
          "derivation": "水平面上仅摩擦力做负功,a₂ = μ₂g = 5 m/s²,滑行 s₂ = 1.6 m 后停止。",
          "why": "水平面上支持力等于重力,滑动摩擦力恒定。",
          "bindPhase": "phase_horizontal"
        },
        {
          "id": "s4",
          "title": "能量核对",
          "law": "能量守恒定律",
          "latex": "mgL\\sin\\theta = \\mu_1 mgL\\cos\\theta + \\mu_2 mg s_2",
          "derivation": "重力势能 12 J = 斜面摩擦生热 4 J + 水平面摩擦生热 8 J,两侧一致(动能最终为零)。",
          "why": "全程能量守恒,可用于检验结果。",
          "bindEvent": "stop"
        }
      ],
      "finalConclusion": "(1) v₁ = 4 m/s;(2) s₂ = 1.6 m。",
      "commonMistakes": [
        "斜面上忘记乘 cosθ 计算支持力,把摩擦力写成 μ₁mg",
        "误用机械能守恒(斜面有摩擦,机械能不守恒)",
        "混淆路程与位移",
        "拐角处误以为速度方向不变会损失能量(本题设平滑连接)"
      ]
    },
    "visualization": {
      "builder": "incline",
      "params": {
        "g": 10,
        "thetaDeg": 37,
        "L": 2,
        "mu1": 0.25,
        "mu2": 0.5,
        "m": 1
      },
      "exploreParams": [
        {
          "key": "thetaDeg",
          "label": "斜面倾角 θ",
          "min": 20,
          "max": 60,
          "step": 1,
          "unit": "°"
        },
        {
          "key": "mu1",
          "label": "斜面摩擦因数 μ₁",
          "min": 0,
          "max": 0.6,
          "step": 0.05,
          "unit": ""
        },
        {
          "key": "mu2",
          "label": "水平面摩擦因数 μ₂",
          "min": 0.1,
          "max": 0.9,
          "step": 0.05,
          "unit": ""
        },
        {
          "key": "L",
          "label": "斜面长 L",
          "min": 1,
          "max": 4,
          "step": 0.5,
          "unit": "m"
        }
      ],
      "schematicParams": false
    }
  },
  {
    "id": "demo-car-brake",
    "title": "汽车匀减速停车(含反应时间)",
    "modules": [
      "运动学"
    ],
    "difficulty": 1,
    "question": "汽车以 v₀=20 m/s 在平直公路上匀速行驶,驾驶员发现前方障碍物,经 t₀=0.5 s 反应时间后开始制动,制动过程视为匀减速直线运动,减速度大小 a=5 m/s²。求:",
    "subQuestions": [
      {
        "id": "q1",
        "text": "(1) 汽车从发现障碍物到停止的总时间;",
        "answer": {
          "value": 4.5,
          "unit": "s",
          "tolerance": 0.02
        }
      },
      {
        "id": "q2",
        "text": "(2) 汽车从发现障碍物到停止的总位移。",
        "answer": {
          "value": 50,
          "unit": "m",
          "tolerance": 0.02
        }
      }
    ],
    "answerSummary": "总时间 4.5 s;总位移 50 m(反应段 10 m + 制动段 40 m)。",
    "solution": {
      "topics": [
        "匀速直线运动",
        "匀变速直线运动"
      ],
      "assumptions": [
        "汽车视为质点",
        "制动过程为理想匀减速"
      ],
      "modelDescription": "运动分两个阶段:反应时间内匀速;制动后匀减速到停止。",
      "steps": [
        {
          "id": "s1",
          "title": "反应阶段位移",
          "law": "匀速直线运动",
          "latex": "s_0 = v_0 t_0 = 20\\times0.5 = 10\\ \\text{m}",
          "derivation": "反应时间内车速不变,s₀ = 10 m。",
          "why": "驾驶员未踩刹车前无制动力,汽车保持匀速。",
          "bindPhase": "phase_reaction"
        },
        {
          "id": "s2",
          "title": "制动阶段时间与位移",
          "law": "匀变速直线运动公式",
          "latex": "t_1 = \\dfrac{v_0}{a} = 4\\ \\text{s},\\quad s_1 = \\dfrac{v_0^2}{2a} = 40\\ \\text{m}",
          "derivation": "v = v₀ − at = 0 → t₁ = 4 s;s₁ = v₀²/(2a) = 40 m。",
          "why": "减速度恒定,末速度为零。",
          "bindPhase": "phase_brake"
        },
        {
          "id": "s3",
          "title": "求总量",
          "law": "分段求和",
          "latex": "t = t_0 + t_1 = 4.5\\ \\text{s},\\quad s = s_0 + s_1 = 50\\ \\text{m}",
          "derivation": "总时间 4.5 s,总位移 50 m。",
          "why": "两阶段依次发生,直接相加。",
          "bindEvent": "stop"
        }
      ],
      "finalConclusion": "(1) 4.5 s;(2) 50 m。",
      "commonMistakes": [
        "忽略反应时间内的匀速位移",
        "把 v–t 图面积和斜率混淆",
        "刹车问题倒过来算时误用时间(注意车停后不再运动)"
      ]
    },
    "visualization": {
      "builder": "carBrake",
      "params": {
        "v0": 20,
        "reactionTime": 0.5,
        "decel": 5,
        "m": 1500
      },
      "exploreParams": [
        {
          "key": "v0",
          "label": "初速度 v₀",
          "min": 10,
          "max": 40,
          "step": 2,
          "unit": "m/s"
        },
        {
          "key": "reactionTime",
          "label": "反应时间 t₀",
          "min": 0,
          "max": 1.5,
          "step": 0.1,
          "unit": "s"
        },
        {
          "key": "decel",
          "label": "制动减速度 a",
          "min": 2,
          "max": 10,
          "step": 0.5,
          "unit": "m/s²"
        }
      ],
      "schematicParams": false
    }
  },
  {
    "id": "demo-projectile",
    "title": "平抛运动",
    "modules": [
      "运动学"
    ],
    "difficulty": 2,
    "question": "一小球以 v₀=10 m/s 的水平初速度从高 h=5 m 的平台边缘抛出,忽略空气阻力,取 g=10 m/s²。求:",
    "subQuestions": [
      {
        "id": "q1",
        "text": "(1) 小球在空中飞行的时间;",
        "answer": {
          "value": 1,
          "unit": "s",
          "tolerance": 0.02
        }
      },
      {
        "id": "q2",
        "text": "(2) 落地点到抛出点的水平距离;",
        "answer": {
          "value": 10,
          "unit": "m",
          "tolerance": 0.02
        }
      },
      {
        "id": "q3",
        "text": "(3) 落地时速度大小。",
        "answer": {
          "value": 14.14,
          "unit": "m/s",
          "expression": "10\\sqrt{2}",
          "tolerance": 0.02
        }
      }
    ],
    "answerSummary": "t = 1 s;x = 10 m;v落 = 10√2 ≈ 14.1 m/s(与水平方向成 45°)。",
    "solution": {
      "topics": [
        "平抛运动",
        "运动的合成与分解"
      ],
      "assumptions": [
        "忽略空气阻力",
        "小球视为质点"
      ],
      "modelDescription": "平抛运动分解为水平方向匀速直线运动和竖直方向自由落体运动。",
      "steps": [
        {
          "id": "s1",
          "title": "竖直方向求时间",
          "law": "自由落体运动",
          "latex": "h = \\tfrac{1}{2}gt^2 \\Rightarrow t = \\sqrt{\\tfrac{2h}{g}} = 1\\ \\text{s}",
          "derivation": "竖直方向初速度为零,h=5 m 得 t=1 s。",
          "why": "竖直方向只受重力,是自由落体。",
          "bindPhase": "phase_flight"
        },
        {
          "id": "s2",
          "title": "水平方向求射程",
          "law": "匀速直线运动",
          "latex": "x = v_0 t = 10\\times1 = 10\\ \\text{m}",
          "derivation": "水平方向不受力,x = 10 m。",
          "why": "水平方向无外力,速度保持 v₀。",
          "bindEvent": "land"
        },
        {
          "id": "s3",
          "title": "合成落地速度",
          "law": "速度矢量合成",
          "latex": "v = \\sqrt{v_0^2 + (gt)^2} = \\sqrt{100+100} = 10\\sqrt{2}\\ \\text{m/s}",
          "derivation": "落地时 vy = gt = 10 m/s,与 vx 垂直合成,方向与水平成 45°。",
          "why": "两分运动互不影响,末速度为矢量和。",
          "bindEvent": "land"
        }
      ],
      "finalConclusion": "t=1 s;x=10 m;v=10√2 m/s,方向与水平方向成 45° 斜向下。",
      "commonMistakes": [
        "误认为水平初速度影响下落时间(时间只由高度决定)",
        "落地速度只算竖直分量,忘记矢量合成",
        "轨迹当成圆弧(应为抛物线)"
      ]
    },
    "visualization": {
      "builder": "projectile",
      "params": {
        "h": 5,
        "v0": 10,
        "g": 10,
        "m": 0.5
      },
      "exploreParams": [
        {
          "key": "h",
          "label": "高度 h",
          "min": 2,
          "max": 10,
          "step": 0.5,
          "unit": "m"
        },
        {
          "key": "v0",
          "label": "初速度 v₀",
          "min": 4,
          "max": 16,
          "step": 1,
          "unit": "m/s"
        }
      ],
      "schematicParams": false
    }
  },
  {
    "id": "demo-pendulum",
    "title": "单摆的摆动",
    "modules": [
      "振动与波",
      "功和能"
    ],
    "difficulty": 2,
    "question": "一单摆摆长 L=1 m,将摆球拉至与竖直方向成 15° 角后由静止释放(g 取 9.8 m/s²)。关于摆球运动,下列说法正确的是:",
    "subQuestions": [],
    "answerSummary": "正确选项:C。",
    "solution": {
      "topics": [
        "单摆",
        "简谐运动",
        "机械能守恒",
        "向心加速度"
      ],
      "assumptions": [
        "摆线不可伸长且质量不计",
        "忽略空气阻力",
        "摆球视为质点"
      ],
      "modelDescription": "摆球绕悬点做变速圆周运动;切向由重力分量提供加速度,法向由张力与重力分量提供向心力。",
      "steps": [
        {
          "id": "s1",
          "title": "最低点分析",
          "law": "向心力公式",
          "latex": "T - mg = \\dfrac{mv^2}{L}",
          "derivation": "最低点速度最大,向心加速度 a=v²/L ≠ 0,故 A 错。",
          "why": "圆周运动中即使速率最大,方向也在改变,加速度不为零。",
          "bindEvent": "lowest"
        },
        {
          "id": "s2",
          "title": "最高点分析",
          "law": "切向牛顿第二定律",
          "latex": "a_t = g\\sin\\theta_0 \\neq 0",
          "derivation": "最高点 v=0 但切向加速度不为零,摆球随即向回摆动,故 B 错。这是'速度为零≠加速度为零'的典型情形。",
          "why": "加速度由合力决定,与瞬时速度无关。",
          "bindEvent": "far_side"
        },
        {
          "id": "s3",
          "title": "能量分析",
          "law": "机械能守恒定律",
          "latex": "\\tfrac{1}{2}mv^2 + mgh = \\text{const}",
          "derivation": "张力处处垂直于速度,不做功;动能与重力势能相互转化,总量不变,故 C 对。观察动画中能量柱状图。",
          "why": "只有重力做功时机械能守恒。",
          "bindPhase": "phase_swing"
        },
        {
          "id": "s4",
          "title": "周期分析",
          "law": "单摆周期公式(小摆角)",
          "latex": "T = 2\\pi\\sqrt{\\tfrac{L}{g}} \\approx 2.0\\ \\text{s}",
          "derivation": "小摆角下周期与摆角无关;大摆角时周期略增,故 D 错。",
          "why": "sinθ≈θ 的近似只在小角度成立。",
          "bindEvent": "one_period"
        }
      ],
      "finalConclusion": "正确选项 C。",
      "commonMistakes": [
        "将速度为零误认为加速度为零(B 选项陷阱)",
        "认为最低点加速度为零",
        "忘记张力不做功的条件是张力垂直于速度"
      ]
    },
    "visualization": {
      "builder": "pendulum",
      "params": {
        "L": 1,
        "theta0Deg": 15,
        "g": 9.8,
        "m": 0.2,
        "periods": 2
      },
      "exploreParams": [
        {
          "key": "L",
          "label": "摆长 L",
          "min": 0.5,
          "max": 2,
          "step": 0.1,
          "unit": "m"
        },
        {
          "key": "theta0Deg",
          "label": "初始摆角",
          "min": 5,
          "max": 60,
          "step": 5,
          "unit": "°"
        }
      ],
      "schematicParams": false
    }
  },
  {
    "id": "demo-collision",
    "title": "一维弹性碰撞",
    "modules": [
      "动量"
    ],
    "difficulty": 3,
    "question": "光滑水平面上,质量 m₁=2 kg 的小车以 v₁=3 m/s 向右运动,与前方静止的质量 m₂=1 kg 的小车发生正碰,碰撞为弹性碰撞(碰撞时间极短)。求碰后两车的速度。",
    "subQuestions": [
      {
        "id": "q1",
        "text": "(1) 碰后 m₁ 的速度 v₁′;",
        "answer": {
          "value": 1,
          "unit": "m/s",
          "tolerance": 0.02
        }
      },
      {
        "id": "q2",
        "text": "(2) 碰后 m₂ 的速度 v₂′。",
        "answer": {
          "value": 4,
          "unit": "m/s",
          "tolerance": 0.02
        }
      }
    ],
    "answerSummary": "v₁′ = 1 m/s(向右),v₂′ = 4 m/s(向右)。",
    "solution": {
      "topics": [
        "动量守恒定律",
        "弹性碰撞"
      ],
      "assumptions": [
        "水平面光滑",
        "碰撞时间极短,碰撞过程中位移可忽略",
        "正碰(一维)"
      ],
      "modelDescription": "以两车为系统,碰撞内力远大于外力,动量守恒;弹性碰撞动能也守恒。",
      "steps": [
        {
          "id": "s1",
          "title": "动量守恒方程",
          "law": "动量守恒定律",
          "latex": "m_1v_1 = m_1v_1' + m_2v_2'",
          "derivation": "2×3 = 2v₁′ + 1×v₂′。",
          "why": "碰撞瞬间系统所受合外力(重力与支持力抵消)为零。",
          "bindEvent": "collide"
        },
        {
          "id": "s2",
          "title": "动能守恒方程",
          "law": "弹性碰撞动能守恒",
          "latex": "\\tfrac{1}{2}m_1v_1^2 = \\tfrac{1}{2}m_1v_1'^2 + \\tfrac{1}{2}m_2v_2'^2",
          "derivation": "9 = v₁′² + ½v₂′²。",
          "why": "题设为弹性碰撞。",
          "bindEvent": "collide"
        },
        {
          "id": "s3",
          "title": "解方程组",
          "law": "弹性碰撞公式",
          "latex": "v_1' = \\dfrac{m_1-m_2}{m_1+m_2}v_1 = 1\\ \\text{m/s},\\quad v_2' = \\dfrac{2m_1}{m_1+m_2}v_1 = 4\\ \\text{m/s}",
          "derivation": "代入 m₁=2, m₂=1, v₁=3 得 v₁′=1 m/s、v₂′=4 m/s,均向右。",
          "why": "两守恒方程联立的标准解(舍去'未碰'解 v₁′=v₁)。",
          "bindPhase": "phase_after"
        }
      ],
      "finalConclusion": "碰后 m₁ 以 1 m/s、m₂ 以 4 m/s 沿原方向运动。",
      "commonMistakes": [
        "只用动量守恒一个方程就求两个未知数",
        "保留 v₁′=v₁ 的增根(对应没有发生碰撞)",
        "碰后 m₁ 速度大于 m₂ 时未检验是否二次相碰(本题 v₁′<v₂′ 合理)",
        "动量是矢量,忘记规定正方向"
      ]
    },
    "visualization": {
      "builder": "collision",
      "params": {
        "m1": 2,
        "v1": 3,
        "m2": 1,
        "v2": 0,
        "e": 1,
        "gap": 2
      },
      "exploreParams": [
        {
          "key": "m1",
          "label": "质量 m₁",
          "min": 0.5,
          "max": 4,
          "step": 0.5,
          "unit": "kg"
        },
        {
          "key": "m2",
          "label": "质量 m₂",
          "min": 0.5,
          "max": 4,
          "step": 0.5,
          "unit": "kg"
        },
        {
          "key": "v1",
          "label": "初速度 v₁",
          "min": 1,
          "max": 6,
          "step": 0.5,
          "unit": "m/s"
        },
        {
          "key": "e",
          "label": "恢复系数 e",
          "min": 0,
          "max": 1,
          "step": 0.1,
          "unit": ""
        }
      ],
      "schematicParams": false
    }
  },
  {
    "id": "demo-conveyor",
    "title": "传送带与滑块",
    "modules": [
      "动力学",
      "功和能"
    ],
    "difficulty": 3,
    "question": "水平传送带以 v带=4 m/s 匀速向右运动,带长 L=6 m。将一质量 m=1 kg 的物块无初速度地放在带的左端,物块与带间动摩擦因数 μ=0.4,取 g=10 m/s²。求:",
    "subQuestions": [
      {
        "id": "q1",
        "text": "(1) 物块与传送带达到共速所需时间及此过程物块位移;",
        "answer": {
          "value": 1,
          "unit": "s",
          "tolerance": 0.02,
          "scoringPoints": [
            "a=μg=4 m/s²",
            "t=v/a=1 s",
            "s=2 m"
          ]
        }
      },
      {
        "id": "q2",
        "text": "(2) 物块从放上到离开传送带的总时间;",
        "answer": {
          "value": 2,
          "unit": "s",
          "tolerance": 0.02
        }
      },
      {
        "id": "q3",
        "text": "(3) 全程摩擦产生的热量。",
        "answer": {
          "value": 8,
          "unit": "J",
          "expression": "Q=\\mu mg\\cdot\\Delta s_{相对}",
          "tolerance": 0.02
        }
      }
    ],
    "answerSummary": "共速时间 1 s(位移 2 m);总时间 2 s;摩擦热 Q = 8 J(相对滑动 2 m)。",
    "solution": {
      "topics": [
        "滑动摩擦",
        "相对运动",
        "功能关系"
      ],
      "assumptions": [
        "物块视为质点",
        "带速恒定",
        "共速后无相对滑动趋势(水平带)"
      ],
      "modelDescription": "物块初速为零、带向右运动,摩擦力向右使物块加速;共速后水平方向无摩擦,物块随带匀速。",
      "steps": [
        {
          "id": "s1",
          "title": "共速前:摩擦加速",
          "law": "牛顿第二定律",
          "latex": "a = \\mu g = 4\\ \\text{m/s}^2,\\quad t_1 = \\dfrac{v_带}{a} = 1\\ \\text{s},\\quad s_1 = \\dfrac{v_带^2}{2a} = 2\\ \\text{m}",
          "derivation": "物块相对带向左滑动,滑动摩擦力向右;1 s 后达到 4 m/s,位移 2 m。",
          "why": "相对滑动存在时摩擦力为滑动摩擦力,方向使相对速度减小。",
          "bindPhase": "phase_slide"
        },
        {
          "id": "s2",
          "title": "共速后:随带匀速",
          "law": "运动学",
          "latex": "t_2 = \\dfrac{L - s_1}{v_带} = \\dfrac{4}{4} = 1\\ \\text{s}",
          "derivation": "共速后摩擦力消失(水平带无驱动需求),物块匀速走完剩余 4 m,再用 1 s;总时间 2 s。",
          "why": "共速后无相对滑动,水平方向不需要摩擦力。",
          "bindEvent": "relative_velocity_zero"
        },
        {
          "id": "s3",
          "title": "摩擦生热",
          "law": "功能关系 Q = f·Δs相对",
          "latex": "\\Delta s_{相对} = v_带 t_1 - s_1 = 4 - 2 = 2\\ \\text{m},\\quad Q = \\mu mg \\cdot \\Delta s_{相对} = 8\\ \\text{J}",
          "derivation": "共速前带走了 4 m、物块走了 2 m,相对滑动 2 m('划痕'长度),Q = 4 N × 2 m = 8 J。",
          "why": "摩擦热由相对滑动路程决定,而不是物块位移。",
          "bindEvent": "exit_conveyor"
        }
      ],
      "finalConclusion": "(1) 1 s,2 m;(2) 2 s;(3) 8 J。",
      "commonMistakes": [
        "用物块位移而非相对滑动路程计算摩擦热",
        "共速后仍认为有摩擦力使物块继续加速",
        "没有判断物块是否在共速前已到达带末端"
      ]
    },
    "visualization": {
      "builder": "conveyor",
      "params": {
        "v0": 0,
        "vBelt": 4,
        "mu": 0.4,
        "g": 10,
        "L": 6,
        "m": 1
      },
      "exploreParams": [
        {
          "key": "vBelt",
          "label": "带速 v带",
          "min": 1,
          "max": 8,
          "step": 0.5,
          "unit": "m/s"
        },
        {
          "key": "v0",
          "label": "物块初速 v₀",
          "min": 0,
          "max": 8,
          "step": 0.5,
          "unit": "m/s"
        },
        {
          "key": "mu",
          "label": "动摩擦因数 μ",
          "min": 0.1,
          "max": 0.8,
          "step": 0.05,
          "unit": ""
        },
        {
          "key": "L",
          "label": "带长 L",
          "min": 3,
          "max": 10,
          "step": 0.5,
          "unit": "m"
        }
      ],
      "schematicParams": false
    }
  },
  {
    "id": "demo-loop",
    "title": "竖直圆轨道与脱轨判断",
    "modules": [
      "圆周运动",
      "功和能"
    ],
    "difficulty": 4,
    "question": "如图,小球从高 H 处由静止沿光滑轨道下滑,进入半径 R=0.5 m 的竖直圆轨道内侧(g 取 10 m/s²)。下列说法正确的是:",
    "subQuestions": [],
    "answerSummary": "正确选项:B。",
    "solution": {
      "topics": [
        "圆周运动",
        "机械能守恒",
        "脱轨条件"
      ],
      "assumptions": [
        "轨道光滑",
        "小球视为质点",
        "内侧轨道只能提供指向圆心的支持力"
      ],
      "modelDescription": "小球沿光滑轨道下滑,机械能守恒;在圆轨道上方,支持力 N 与重力分量共同提供向心力,N 减为零即脱轨。",
      "steps": [
        {
          "id": "s1",
          "title": "最高点最小速度",
          "law": "向心力公式(最高点)",
          "latex": "mg = \\dfrac{mv_{min}^2}{R} \\Rightarrow v_{min} = \\sqrt{gR}",
          "derivation": "N=0 时重力全部提供向心力,v_min = √(10×0.5) ≈ 2.24 m/s。",
          "why": "内侧轨道不能拉球,N≥0,故最高点存在最小速度。",
          "bindEvent": "reach_top"
        },
        {
          "id": "s2",
          "title": "临界释放高度",
          "law": "机械能守恒",
          "latex": "mgH = mg\\cdot2R + \\tfrac{1}{2}mv_{min}^2 \\Rightarrow H_{临界} = 2.5R",
          "derivation": "H = 2R + R/2 = 2.5R = 1.25 m,故 A 错、B 对。",
          "why": "轨道光滑,只有重力做功。",
          "bindPhase": "phase_circle"
        },
        {
          "id": "s3",
          "title": "H 不足时的脱轨位置",
          "law": "N=0 条件 + 能量守恒",
          "latex": "\\cos\\varphi = \\dfrac{2(H-R)}{3R}",
          "derivation": "在与最高点夹角 φ 处 N=0 脱轨,随后做抛体运动;并非在最高点脱离,故 C 错。可在探索模式中把 H 调小观察。",
          "why": "联立 mg·cosφ = mv²/R 与能量守恒即得。",
          "bindEvent": "leave_track"
        },
        {
          "id": "s4",
          "title": "最低点超重",
          "law": "向心力公式(最低点)",
          "latex": "N - mg = \\dfrac{mv^2}{R} \\Rightarrow N > mg",
          "derivation": "最低点向心加速度向上,支持力大于重力,是超重不是失重,故 D 错。",
          "why": "向心加速度方向指向圆心(向上)。",
          "bindEvent": "enter_circle"
        }
      ],
      "finalConclusion": "正确选项 B。",
      "commonMistakes": [
        "误以为 H>2R 就能通过最高点,忽略最高点最小速度 √(gR)",
        "认为速度不足时小球会滑到最高点才脱离(实际在 N=0 处提前脱轨)",
        "把内侧轨道当成管道(管道外壁可提供向内的力,无最小速度限制)"
      ]
    },
    "visualization": {
      "builder": "loop",
      "params": {
        "H": 1.5,
        "R": 0.5,
        "g": 10,
        "m": 0.2,
        "slopeDeg": 50
      },
      "exploreParams": [
        {
          "key": "H",
          "label": "释放高度 H",
          "min": 0.7,
          "max": 2.2,
          "step": 0.05,
          "unit": "m"
        },
        {
          "key": "R",
          "label": "圆轨道半径 R",
          "min": 0.3,
          "max": 0.7,
          "step": 0.05,
          "unit": "m"
        }
      ],
      "schematicParams": false
    }
  },
  {
    "id": "demo-wave",
    "title": "机械波的传播与叠加",
    "modules": [
      "振动与波"
    ],
    "difficulty": 3,
    "question": "一根张紧的弦上,两端波源 S₁、S₂ 同时开始振动,各产生一列振幅 A=0.2 m、波长 λ=2 m、波速 v=2 m/s 的简谐横波相向传播,弦长 L=12 m。下列说法正确的是:",
    "subQuestions": [],
    "answerSummary": "正确选项:B。",
    "solution": {
      "topics": [
        "机械波",
        "波的叠加",
        "干涉加强与减弱"
      ],
      "assumptions": [
        "弦均匀,波速恒定",
        "线性叠加(小振幅)"
      ],
      "modelDescription": "两列同频同幅横波相向传播,在重叠区内位移按矢量和叠加。",
      "steps": [
        {
          "id": "s1",
          "title": "波前相遇时间",
          "law": "匀速传播",
          "latex": "t = \\dfrac{L}{2v} = \\dfrac{12}{4} = 3\\ \\text{s}",
          "derivation": "两波前各走 6 m 在中点相遇,t=3 s,故 C 错。",
          "why": "机械波在均匀介质中匀速传播。",
          "bindEvent": "fronts_meet"
        },
        {
          "id": "s2",
          "title": "中点的叠加",
          "law": "叠加原理",
          "latex": "\\Delta r = 0 \\Rightarrow 振幅 = 2A",
          "derivation": "中点到两波源路程差为零,两波到达时总同相,合振幅 2A = 0.4 m,故 B 对。动画中观察中点位移曲线。",
          "why": "同相叠加时位移代数和最大。",
          "bindPhase": "phase_overlap"
        },
        {
          "id": "s3",
          "title": "独立传播与其他点",
          "law": "波的独立传播原理",
          "latex": "y = y_1 + y_2",
          "derivation": "相遇后各波保持各自波形继续传播,故 A 错;路程差 Δr = (2k+1)λ/2 的点振动减弱,故 D 错。",
          "why": "线性介质中波互不干扰,只在重叠区位移相加。",
          "bindPhase": "phase_overlap"
        }
      ],
      "finalConclusion": "正确选项 B。",
      "commonMistakes": [
        "认为波相遇后会'相互抵消消失'(减弱点位移为零但能量仍在传播)",
        "相遇时间用 L/v 而忘记两波相向而行",
        "把加强点误解为'一直位于最大位移处'(加强点仍在振动,只是振幅大)"
      ]
    },
    "visualization": {
      "builder": "wave",
      "params": {
        "A": 0.2,
        "lambda": 2,
        "v": 2,
        "span": 12
      },
      "exploreParams": [
        {
          "key": "lambda",
          "label": "波长 λ",
          "min": 1,
          "max": 4,
          "step": 0.5,
          "unit": "m"
        },
        {
          "key": "v",
          "label": "波速 v",
          "min": 1,
          "max": 4,
          "step": 0.5,
          "unit": "m/s"
        },
        {
          "key": "A",
          "label": "振幅 A",
          "min": 0.1,
          "max": 0.4,
          "step": 0.05,
          "unit": "m"
        }
      ],
      "schematicParams": false
    }
  },
  {
    "id": "demo-refraction",
    "title": "光的折射与全反射",
    "modules": [
      "光学"
    ],
    "difficulty": 3,
    "question": "一束光从折射率 n=1.5 的玻璃内部射向玻璃与空气的水平界面,入射角 θ₁ 从 10° 逐渐增大到 70°。下列说法正确的是:",
    "subQuestions": [],
    "answerSummary": "正确选项:B。",
    "solution": {
      "topics": [
        "折射定律",
        "全反射",
        "临界角"
      ],
      "assumptions": [
        "界面平整",
        "单色光",
        "几何光学近似"
      ],
      "modelDescription": "光从光密介质(玻璃)射向光疏介质(空气),随入射角增大,折射角先增大到 90°,随后发生全反射。",
      "steps": [
        {
          "id": "s1",
          "title": "折射定律",
          "law": "斯涅尔定律",
          "latex": "n\\sin\\theta_1 = \\sin\\theta_2",
          "derivation": "n=1.5>1,故 sinθ₂ = 1.5·sinθ₁ > sinθ₁,折射角大于入射角,A 错。",
          "why": "折射率定义与折射定律。",
          "bindPhase": "phase_refract"
        },
        {
          "id": "s2",
          "title": "临界角",
          "law": "全反射条件",
          "latex": "\\sin\\theta_c = \\dfrac{1}{n} = \\dfrac{2}{3} \\Rightarrow \\theta_c \\approx 41.8°",
          "derivation": "θ₂=90° 时对应的入射角即临界角;θ₁>θc 后无折射光,B 对、C 错。观察动画中折射光消失的时刻。",
          "why": "光密→光疏且入射角超过临界角是全反射的条件。",
          "bindEvent": "total_internal_reflection"
        },
        {
          "id": "s3",
          "title": "介质中光速",
          "law": "v = c/n",
          "latex": "v_{玻璃} = \\dfrac{c}{1.5} = 2\\times10^8\\ \\text{m/s} < c",
          "derivation": "折射率越大光速越小,D 错。",
          "why": "折射率的物理意义。",
          "bindPhase": "phase_refract"
        }
      ],
      "finalConclusion": "正确选项 B。",
      "commonMistakes": [
        "忘记全反射的两个条件缺一不可(光密→光疏 + 超过临界角)",
        "认为全反射后仍有微弱折射光",
        "混淆'折射角大于入射角'的方向条件"
      ]
    },
    "visualization": {
      "builder": "refraction",
      "params": {
        "n1": 1.5,
        "n2": 1,
        "thetaStartDeg": 10,
        "thetaEndDeg": 70,
        "sweepSeconds": 8
      },
      "exploreParams": [
        {
          "key": "n1",
          "label": "介质折射率 n₁",
          "min": 1.2,
          "max": 2.4,
          "step": 0.1,
          "unit": ""
        }
      ],
      "schematicParams": false
    }
  },
  {
    "id": "demo-magnetic",
    "title": "带电粒子进入匀强磁场",
    "modules": [
      "磁场"
    ],
    "difficulty": 4,
    "question": "如图,宽度 d=1 m 的区域内存在垂直纸面向里的匀强磁场。一带正电粒子以速度 v 沿水平方向垂直磁场边界射入,在场内做匀速圆周运动,回转半径 r=2 m(示意参数,归一化 q/m)。求粒子射出磁场时速度方向相对入射方向的偏转角 θ 及侧向位移 Δy。",
    "subQuestions": [
      {
        "id": "q1",
        "text": "(1) 偏转角 θ(度);",
        "answer": {
          "value": 30,
          "unit": "°",
          "expression": "\\sin\\theta = d/r",
          "tolerance": 0.02
        }
      },
      {
        "id": "q2",
        "text": "(2) 侧向位移 Δy(m)。",
        "answer": {
          "value": 0.268,
          "unit": "m",
          "expression": "\\Delta y = r(1-\\cos\\theta)",
          "tolerance": 0.05
        }
      }
    ],
    "answerSummary": "sinθ = d/r = 0.5 → θ = 30°;Δy = r(1−cos30°) ≈ 0.27 m。",
    "solution": {
      "topics": [
        "洛伦兹力",
        "匀速圆周运动",
        "几何法求偏转"
      ],
      "assumptions": [
        "磁场区域内匀强、边界外无场",
        "忽略重力(微观粒子)",
        "粒子视为质点"
      ],
      "modelDescription": "洛伦兹力始终垂直于速度,提供向心力,粒子在场内做匀速圆周运动;出场后沿切线方向匀速直线运动。",
      "steps": [
        {
          "id": "s1",
          "title": "判断偏转方向与半径",
          "law": "洛伦兹力 F = qv×B",
          "latex": "qvB = \\dfrac{mv^2}{r} \\Rightarrow r = \\dfrac{mv}{qB}",
          "derivation": "正电荷、v 向右、B 向里 → F 向上,粒子向上偏转;圆心在入射点正上方距离 r 处。",
          "why": "洛伦兹力垂直于速度,只改变方向不改变速率,做匀速圆周运动。",
          "bindEvent": "enter_field"
        },
        {
          "id": "s2",
          "title": "几何关系求偏转角",
          "law": "圆的几何关系",
          "latex": "\\sin\\theta = \\dfrac{d}{r} = \\dfrac{1}{2} \\Rightarrow \\theta = 30°",
          "derivation": "弦所对圆心角等于速度偏转角;水平穿越宽度 d 时 sinθ = d/r。",
          "why": "圆周运动中速度方向转过的角度等于对应圆心角。",
          "bindPhase": "phase_in_field"
        },
        {
          "id": "s3",
          "title": "侧向位移",
          "law": "圆的几何关系",
          "latex": "\\Delta y = r(1-\\cos\\theta) = 2\\times(1-\\tfrac{\\sqrt{3}}{2}) \\approx 0.27\\ \\text{m}",
          "derivation": "由圆心几何直接得出。出场后沿切线做匀速直线运动。",
          "why": "位置坐标由圆方程给出。",
          "bindEvent": "exit_field"
        }
      ],
      "finalConclusion": "θ = 30°;Δy ≈ 0.27 m;粒子速率始终不变(洛伦兹力不做功)。",
      "commonMistakes": [
        "忘记洛伦兹力不做功,误算动能变化",
        "左手定则判断力方向时把电流方向与电子运动方向弄反(本题为正电荷)",
        "把偏转角误当作与边界的夹角",
        "圆心位置找错(圆心在垂直于初速度的方向上)"
      ]
    },
    "visualization": {
      "builder": "magnetic",
      "params": {
        "q": 1,
        "m": 1,
        "v": 2,
        "B": 1,
        "d": 1,
        "fieldHeight": 3.2
      },
      "exploreParams": [
        {
          "key": "B",
          "label": "磁感应强度 B(示意)",
          "min": 0.5,
          "max": 1.8,
          "step": 0.1,
          "unit": "T"
        },
        {
          "key": "v",
          "label": "入射速度 v(示意)",
          "min": 1,
          "max": 4,
          "step": 0.25,
          "unit": "m/s"
        }
      ],
      "schematicParams": true
    }
  }
];

export default DEMOS;
