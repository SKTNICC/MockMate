package com.mockmate.app.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.mockmate.app.ui.AppState
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.components.ConsentDialog
import com.mockmate.app.ui.components.InfoCard
import com.mockmate.app.ui.components.MessageBanner
import com.mockmate.app.ui.components.PrimaryAction
import com.mockmate.app.ui.components.SecondaryAction

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AuthScreen(vm: AppViewModel, state: AppState) {
    var registerMode by rememberSaveable { mutableStateOf(false) }
    var username by rememberSaveable { mutableStateOf("") }
    var password by rememberSaveable { mutableStateOf("") }
    var showNote by rememberSaveable { mutableStateOf(false) }

    Scaffold(topBar = { TopAppBar(title = { Text("MockMate 模拟面试") }) }) { padding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .imePadding()
                .padding(horizontal = 20.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                Spacer(Modifier.height(12.dp))
                Text("M1：面试准备与个性化计划", style = MaterialTheme.typography.titleLarge)
                Text(
                    "登录后准备并核对简历与 JD，查看有证据的匹配结果，生成并保存一份 3+3 计划。",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            item {
                InfoCard(title = if (registerMode) "注册新账号" else "登录") {
                    OutlinedTextField(
                        value = username,
                        onValueChange = { username = it },
                        label = { Text("用户名（3–32 位字母、数字或下划线）") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
                    )
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = password,
                        onValueChange = { password = it },
                        label = { Text("密码（10–128 字符）") },
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        keyboardOptions = KeyboardOptions(
                            keyboardType = KeyboardType.Password,
                            imeAction = ImeAction.Done,
                        ),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(12.dp))
                    PrimaryAction(
                        text = if (registerMode) "注册并登录" else "登录",
                        onClick = { vm.authenticate(registerMode, username, password) },
                        enabled = !state.loading,
                    )
                    Spacer(Modifier.height(6.dp))
                    TextButton(onClick = { registerMode = !registerMode }) {
                        Text(if (registerMode) "已有账号？去登录" else "没有账号？注册")
                    }
                }
            }
            item {
                InfoCard(title = "本轮范围", subtitle = "只实现 M1；不含面试问答、评分报告、语音或画像") {
                    Text(
                        "材料缺失或未确认时服务端会阻止分析与生成；题库与生成题严格各半；" +
                            "本页不展示题干与答案。计划保存后可在重新登录后读回。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Spacer(Modifier.height(8.dp))
                    TextButton(onClick = { showNote = true }) { Text("数据与授权说明") }
                }
            }
        }
        if (showNote) {
            ConsentDialog(
                title = "数据与授权说明",
                body = "简历/JD 与账号资料只保存在本服务端。只有在你确认后，解析、匹配与生成才会把材料文本发送给配置的外部模型；" +
                    "原文件不会发送。计划页只显示摘要，不显示题干与答案。",
                confirmLabel = "我知道了",
                onConfirm = { showNote = false },
                onDismiss = { showNote = false },
            )
        }
    }
}
