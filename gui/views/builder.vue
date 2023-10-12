<script setup>
import { onMounted, ref } from "vue";
const $api = inject("$api");

const envs = ref();

onMounted(async () => {
  try {
    envs = await $api.get("/plugin/builder/environment");
  } catch (error) {
    console.error(error);
  }
});
</script>

<template lang="pug">
.content    
  h2 Builder
  p Dynamically compile ability code via docker containershr

.content
<!-- a table with a list of envs -->
  table.table.is-fullwidth
    thead
      tr
        th Name
        th Description
        th
    tbody
      tr(v-for="env in envs")
        td {{ env }}
.is-flex.is-align-items-center.is-justify-content-center
    a.button.is-primary(href="/docs/Dynamically-Compiled-Payloads.html" target="_blank")
        span Read more about using Builder to create dynamically-compiled payloads here:
        span.icon
            font-awesome-icon(icon="fas fa-angle-right")

</template>
