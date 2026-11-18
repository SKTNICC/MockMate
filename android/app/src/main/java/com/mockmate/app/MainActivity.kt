package com.mockmate.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import com.mockmate.app.ui.AppRoot
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.theme.MockMateTheme

/**
 * M1 单 Activity 入口：登录 → 准备 → 材料核对 → 匹配 → 计划确认 → 重开。
 * 计划页只显示摘要；不包含任何题干、答案或评分细则。
 */
class MainActivity : ComponentActivity() {
    private val appViewModel: AppViewModel by viewModels { AppViewModel.factory(application) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MockMateTheme {
                AppRoot(appViewModel)
            }
        }
    }

    override fun onStart() {
        super.onStart()
        appViewModel.setAppVisible(true)
    }

    override fun onStop() {
        appViewModel.setAppVisible(false)
        super.onStop()
    }
}
