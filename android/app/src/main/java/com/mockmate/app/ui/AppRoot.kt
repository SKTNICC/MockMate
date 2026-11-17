package com.mockmate.app.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.mockmate.app.ui.components.LoadingRow
import com.mockmate.app.ui.components.MessageBanner
import com.mockmate.app.ui.screens.AuthScreen
import com.mockmate.app.ui.screens.EditorScreen
import com.mockmate.app.ui.screens.InterviewHistoryScreen
import com.mockmate.app.ui.screens.InterviewReportScreen
import com.mockmate.app.ui.screens.InterviewRoomScreen
import com.mockmate.app.ui.screens.LibraryScreen
import com.mockmate.app.ui.screens.MatchScreen
import com.mockmate.app.ui.screens.PlanHubScreen
import com.mockmate.app.ui.screens.PreparationListScreen
import com.mockmate.app.ui.screens.ReviewScreen
import com.mockmate.app.ui.screens.SetupScreen
import com.mockmate.app.ui.screens.TemplatesScreen
import kotlinx.coroutines.delay

@Composable
fun AppRoot(vm: AppViewModel) {
    val state by vm.state
    BackHandler(enabled = state.stack.size > 1) { vm.back() }

    val message = state.message
    LaunchedEffect(message) {
        if (message != null && !message.isError) {
            delay(4000)
            vm.dismissMessage()
        }
    }

    Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(Modifier.fillMaxSize()) {
            if (message != null) {
                Box(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp)) {
                    MessageBanner(message) { vm.dismissMessage() }
                }
            }
            Box(Modifier.fillMaxSize()) {
                when (val top = state.stack.last()) {
                    is Screen.Auth -> AuthScreen(vm, state)
                    is Screen.PreparationList -> PreparationListScreen(vm, state)
                    is Screen.Setup -> SetupScreen(vm, state, top.preparationId)
                    is Screen.Library -> LibraryScreen(vm, state, top.kind, top.preparationId)
                    is Screen.Editor -> EditorScreen(vm, state, top.kind, top.materialId, top.preparationId)
                    is Screen.Review -> ReviewScreen(vm, state, top.materialId, top.preparationId)
                    is Screen.Templates -> TemplatesScreen(vm, state, top.preparationId)
                    is Screen.Match -> MatchScreen(vm, state, top.preparationId)
                    is Screen.PlanHub -> PlanHubScreen(vm, state, top.preparationId)
                    is Screen.InterviewHistory -> InterviewHistoryScreen(vm, state)
                    is Screen.InterviewRoom -> InterviewRoomScreen(vm, state, top.sessionId)
                    is Screen.InterviewReport -> InterviewReportScreen(vm, state, top.sessionId)
                }
                if (state.booting) {
                    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                        Column(
                            Modifier.fillMaxSize(),
                            verticalArrangement = androidx.compose.foundation.layout.Arrangement.Center,
                            horizontalAlignment = Alignment.CenterHorizontally,
                        ) {
                            CircularProgressIndicator()
                            Text("正在恢复会话…", modifier = Modifier.padding(top = 12.dp))
                        }
                    }
                }
                if (state.loading) {
                    Surface(
                        modifier = Modifier.fillMaxSize(),
                        color = MaterialTheme.colorScheme.scrim.copy(alpha = 0.35f),
                    ) {
                        Column(
                            Modifier.fillMaxSize(),
                            verticalArrangement = androidx.compose.foundation.layout.Arrangement.Center,
                            horizontalAlignment = Alignment.CenterHorizontally,
                        ) {
                            Surface(
                                color = MaterialTheme.colorScheme.surface,
                                shape = androidx.compose.foundation.shape.RoundedCornerShape(12.dp),
                            ) {
                                Box(Modifier.padding(20.dp)) {
                                    LoadingRow(state.busyLabel ?: "处理中…")
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
